"""Fixtures for email integration tests.

API tests need a disposable PostgreSQL in EMAIL_TEST_DATABASE_URL (they drop and recreate the email_*
tables) and are skipped without it. Never point it at a shared database. Example:
    docker run -d --rm --name email-test-pg -e POSTGRES_PASSWORD=emailtest -e POSTGRES_DB=email_test
               -p 127.0.0.1:15440:5432 postgres:17-alpine
    set EMAIL_TEST_DATABASE_URL=postgresql://postgres:emailtest@127.0.0.1:15440/email_test
"""
from __future__ import annotations

import base64
import os
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.email_integration import module as email_module

from .fake_providers import FakeProviders

TEST_DSN = os.environ.get("EMAIL_TEST_DATABASE_URL", "")
KEY = base64.b64encode(b"e" * 32).decode()
INTERNAL_KEY = "internal-test-key"
PDF = b"%PDF-1.7\n" + b"0" * 2048

EMAIL_ENV = {
    "EMAIL_TOKEN_ENCRYPTION_KEYS": f'{{"1":"{KEY}"}}', "EMAIL_TOKEN_ENCRYPTION_ACTIVE_KEY": "1",
    "EMAIL_GOOGLE_CLIENT_ID": "google-id", "EMAIL_GOOGLE_CLIENT_SECRET": "google-secret",
    "EMAIL_MICROSOFT_CLIENT_ID": "ms-id", "EMAIL_MICROSOFT_CLIENT_SECRET": "ms-secret",
    "EMAIL_MICROSOFT_TENANT": "common",
    "EMAIL_OAUTH_REDIRECT_URI": "https://gateway.test/api/email/oauth/callback",
    "EMAIL_INTERNAL_KEY": INTERNAL_KEY, "EMAIL_DRAFTS_PER_HOUR": "60",
    # Tests identify sellers by X-User-Id; BigShip verification is covered in test_units.py.
    "EMAIL_SELLER_IDENTITY": "header",
}

requires_db = pytest.mark.skipif(not TEST_DSN, reason="EMAIL_TEST_DATABASE_URL not set")


def reset_db() -> None:
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'email\\_%'")]
        if tables:
            conn.execute("DROP TABLE " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")


def db_rows(sql: str, params=()) -> list[tuple]:
    with psycopg.connect(TEST_DSN) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture
def fake() -> FakeProviders:
    return FakeProviders()


@pytest.fixture
def email_env(monkeypatch, fake):
    for key, value in EMAIL_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("EMAIL_DATABASE_URL", TEST_DSN)
    monkeypatch.setattr(email_module, "PROVIDER_TRANSPORT", fake.transport())
    return EMAIL_ENV


@pytest.fixture
def client(email_env):
    reset_db()
    with TestClient(create_app()) as test_client:
        assert test_client.app.state.email is not None, "email module did not start"
        yield test_client


def seller(seller_id: str = "S1") -> dict[str, str]:
    return {"X-User-Id": seller_id}


def internal(seller_id: str = "S1") -> dict[str, str]:
    return {**seller(seller_id), "X-Email-Internal-Key": INTERNAL_KEY}


def connect(client: TestClient, provider: str = "gmail", seller_id: str = "S1", code: str = "good-code") -> dict:
    """Run the whole popup flow: start -> provider callback -> poll status -> complete."""
    start = client.post(f"/api/email/connect/{provider}/start", headers=seller(seller_id))
    assert start.status_code == 200, start.text
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]
    page = client.get("/api/email/oauth/callback", params={"state": state, "code": code})
    assert page.status_code == 200
    tx = start.json()["transaction_id"]
    assert client.get(f"/api/email/connect/transactions/{tx}", headers=seller(seller_id)).json()["status"] == \
        "authorized"
    done = client.post(f"/api/email/connect/{provider}/complete", json={"transaction_id": tx},
                       headers=seller(seller_id))
    return done.json() | {"_status": done.status_code, "_transaction_id": tx}
