"""Fixtures for Meta integration tests.

Database tests need a disposable PostgreSQL given in META_TEST_DATABASE_URL (they drop and recreate
the meta_* tables). They are skipped when it is unset. Never point it at a shared database.
Example:  docker run -d --rm --name meta-test-pg -e POSTGRES_PASSWORD=metatest -e POSTGRES_DB=meta_test
          -p 127.0.0.1:15439:5432 postgres:17-alpine
          set META_TEST_DATABASE_URL=postgresql://postgres:metatest@127.0.0.1:15439/meta_test
"""
from __future__ import annotations

import base64
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.meta_integration import module as meta_module
from app.modules.meta_integration.worker import MetaWorker

from .fake_meta import FakeMeta

TEST_DSN = os.environ.get("META_TEST_DATABASE_URL", "")
KEY1 = base64.b64encode(b"1" * 32).decode()
KEY2 = base64.b64encode(b"2" * 32).decode()
APP_SECRET = "test-app-secret"
IG_SECRET = "test-ig-secret"
VERIFY_TOKEN = "verify-me"
ADMIN_KEY = "admin-key"

META_ENV = {
    "META_APP_ID": "111", "META_APP_SECRET": APP_SECRET, "META_GRAPH_API_VERSION": "v27.0",
    "META_WEBHOOK_VERIFY_TOKEN": VERIFY_TOKEN,
    "META_OAUTH_REDIRECT_URI": "https://gateway.test/api/meta/oauth/callback",
    "META_FRONTEND_RETURN_URL": "https://panel.test/ai-box/create?tab=sources",
    "META_WHATSAPP_CONFIG_ID": "wa-config", "META_FACEBOOK_CONFIG_ID": "fb-config",
    "META_INSTAGRAM_APP_ID": "222", "META_INSTAGRAM_APP_SECRET": IG_SECRET,
    # Instagram Login mode by default; set explicitly so a local .env cannot switch it.
    "META_INSTAGRAM_CONFIG_ID": "",
    "META_TOKEN_ENCRYPTION_KEYS": f'{{"1":"{KEY1}"}}', "META_TOKEN_ENCRYPTION_ACTIVE_KEY": "1",
    "META_WORKER_ENABLED": "false", "META_ADMIN_API_KEY": ADMIN_KEY,
    "META_MANUAL_SYNC_MIN_INTERVAL_SECONDS": "0", "META_SELLER_RATE_LIMIT_PER_MINUTE": "100",
    "META_PUBLIC_API_BASE_URL": "https://gateway.test/api/meta",
    # Most tests identify sellers by X-User-Id; test_identity.py covers the BigShip-verified mode.
    "META_SELLER_IDENTITY": "header",
}

requires_db = pytest.mark.skipif(not TEST_DSN, reason="META_TEST_DATABASE_URL not set")


def reset_db() -> None:
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'meta\\_%'")]
        if tables:
            conn.execute("DROP TABLE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")


def db_rows(sql: str, params=()) -> list[tuple]:
    with psycopg.connect(TEST_DSN) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture
def fake() -> FakeMeta:
    return FakeMeta()


@pytest.fixture
def meta_env(monkeypatch, fake):
    for key, value in META_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("META_DATABASE_URL", TEST_DSN)
    monkeypatch.setattr(meta_module, "GRAPH_TRANSPORT", fake.transport())
    return META_ENV


@pytest.fixture
def client(meta_env):
    if not TEST_DSN:
        pytest.skip("META_TEST_DATABASE_URL not set")
    reset_db()
    with TestClient(create_app()) as c:
        assert c.app.state.meta is not None, "Meta module failed to start"
        yield c


def seller(seller_id: str) -> dict[str, str]:
    return {"X-User-Id": seller_id}


def run_worker(client: TestClient, ticks: int = 1) -> None:
    worker = MetaWorker(client.app.state.meta.ctx)
    for _ in range(ticks):
        client.portal.call(worker.run_once)


def connect_whatsapp(client: TestClient, seller_id: str = "seller-a", **overrides) -> dict:
    start = client.post("/api/meta/connect/whatsapp/start", headers=seller(seller_id))
    assert start.status_code == 200, start.text
    body = {"transaction_id": start.json()["transaction_id"], "code": "AQB-code-from-fb-login-1234",
            "waba_id": "1000000000000001", "phone_number_id": "2000000000000002", "event": "FINISH", **overrides}
    return client.post("/api/meta/connect/whatsapp/complete", headers=seller(seller_id), json=body)


def connect_redirect(client: TestClient, platform: str, seller_id: str = "seller-a"):
    from urllib.parse import parse_qs, urlparse
    start = client.post(f"/api/meta/connect/{platform}/start", headers=seller(seller_id))
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]
    cb = client.get("/api/meta/oauth/callback", params={"state": state, "code": "AUTHCODE123#_"},
                    follow_redirects=False)
    assert cb.status_code == 302
    tx = start.json()["transaction_id"]
    return client.post(f"/api/meta/connect/{platform}/complete", headers=seller(seller_id),
                       json={"transaction_id": tx}), start, cb
