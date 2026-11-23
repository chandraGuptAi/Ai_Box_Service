"""Seller identity: the seller is whoever BigShip says owns the login token, never a browser-supplied ID."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.meta_integration import module as meta_module
from app.modules.meta_integration.config import MetaSettings
from app.modules.meta_integration.errors import MetaError
from app.modules.meta_integration.identity import PROFILE_PATH, SellerIdentity

from .conftest import META_ENV, TEST_DSN, reset_db

TOKENS = {"Bearer 11|seller-a-token": "1001", "Bearer 12|seller-b-token": "1002"}


class FakeBigship:
    """GET /api/get-user-profile as BigShip answers it (UserController::getUserProfile)."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "ok"

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert request.url.path == PROFILE_PATH
        if self.mode == "down":
            raise httpx.ConnectError("unreachable")
        if self.mode == "garbage":
            return httpx.Response(200, json={"status": "Success", "data": []})
        if self.mode == "failed":
            return httpx.Response(200, json={"status": "Failed", "message": "Account inactive", "data": ""})
        cust = TOKENS.get(request.headers.get("authorization", ""))
        if cust is None:
            return httpx.Response(401, json={"message": "Unauthenticated."})
        return httpx.Response(200, json={"status": "Success", "message": None, "data": {
            "user_profile_data": {"cust_id": int(cust), "first_name": "A", "is_sub_user": False}}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _identity(monkeypatch, fake: FakeBigship, **env) -> SellerIdentity:
    for key, value in {**META_ENV, "META_SELLER_IDENTITY": "bigship",
                       "META_BIGSHIP_API_BASE_URL": "https://bigship.test", **env}.items():
        monkeypatch.setenv(key, value)
    return SellerIdentity(MetaSettings(), fake.transport())


def _code(coro) -> str:
    with pytest.raises(MetaError) as err:
        asyncio.run(coro)
    return err.value.code


def test_seller_comes_from_bigship_not_from_x_user_id(monkeypatch):
    fake = FakeBigship()
    identity = _identity(monkeypatch, fake)
    # The header claims seller B; the token belongs to seller A. The token wins.
    assert asyncio.run(identity.resolve("Bearer 11|seller-a-token", "1002")) == "1001"
    assert asyncio.run(identity.resolve("Bearer 12|seller-b-token", None)) == "1002"


def test_verified_tokens_are_cached_by_hash(monkeypatch):
    fake = FakeBigship()
    identity = _identity(monkeypatch, fake)

    async def twice():
        await identity.resolve("Bearer 11|seller-a-token", None)
        await identity.resolve("Bearer 11|seller-a-token", None)
    asyncio.run(twice())
    assert fake.calls == 1
    assert all("seller-a-token" not in key for key in identity._cache)   # only the SHA-256 is kept


def test_cache_can_be_disabled(monkeypatch):
    fake = FakeBigship()
    identity = _identity(monkeypatch, fake, META_SELLER_IDENTITY_CACHE_SECONDS="0")

    async def twice():
        await identity.resolve("Bearer 11|seller-a-token", None)
        await identity.resolve("Bearer 11|seller-a-token", None)
    asyncio.run(twice())
    assert fake.calls == 2


@pytest.mark.parametrize("header", [None, "", "11|no-bearer-prefix", "Bearer x", "Bearer bad token with spaces"])
def test_missing_or_malformed_login_is_rejected_without_calling_bigship(monkeypatch, header):
    fake = FakeBigship()
    identity = _identity(monkeypatch, fake)
    assert _code(identity.resolve(header, "1001")) == "META_SELLER_UNAUTHENTICATED"
    assert fake.calls == 0


def test_rejected_or_inactive_login_is_unauthenticated(monkeypatch):
    fake = FakeBigship()
    identity = _identity(monkeypatch, fake)
    assert _code(identity.resolve("Bearer 99|expired-token", None)) == "META_SELLER_UNAUTHENTICATED"
    fake.mode = "failed"
    assert _code(identity.resolve("Bearer 11|seller-a-token", None)) == "META_SELLER_UNAUTHENTICATED"


def test_bigship_outage_is_retryable_not_a_logout(monkeypatch):
    fake = FakeBigship()
    identity = _identity(monkeypatch, fake)
    fake.mode = "down"
    with pytest.raises(MetaError) as err:
        asyncio.run(identity.resolve("Bearer 11|seller-a-token", None))
    assert err.value.code == "META_IDENTITY_UNAVAILABLE" and err.value.retryable and err.value.status == 503
    fake.mode = "garbage"
    assert _code(identity.resolve("Bearer 11|seller-a-token", None)) == "META_IDENTITY_UNAVAILABLE"


def test_header_mode_is_local_development_only(monkeypatch):
    for key, value in META_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("META_DATABASE_URL", "postgresql://unused")
    assert MetaSettings().identity_problems() == []
    monkeypatch.setattr(MetaSettings, "allow_http_urls", property(lambda self: False))
    assert MetaSettings().identity_problems() == ["META_SELLER_IDENTITY (header is not allowed in production)"]
    assert "META_SELLER_IDENTITY (header is not allowed in production)" in MetaSettings().platform_problems("whatsapp")


def test_bigship_mode_needs_the_bigship_api(monkeypatch):
    for key, value in META_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("META_SELLER_IDENTITY", "bigship")
    monkeypatch.setenv("META_BIGSHIP_API_BASE_URL", "not a url")
    assert MetaSettings().identity_problems() == ["BIGSHIP_API_BASE_URL"]
    monkeypatch.setenv("META_BIGSHIP_API_BASE_URL", "https://bigship.test")
    assert MetaSettings().identity_problems() == []


# ---- through the API (needs the test database) ------------------------------------------------------

A = {"X-Shipment-Authorization": "Bearer 11|seller-a-token"}
B = {"X-Shipment-Authorization": "Bearer 12|seller-b-token"}


@pytest.fixture
def bigship_client(meta_env, monkeypatch):
    if not TEST_DSN:
        pytest.skip("META_TEST_DATABASE_URL not set")
    fake = FakeBigship()
    monkeypatch.setenv("META_SELLER_IDENTITY", "bigship")
    monkeypatch.setenv("META_BIGSHIP_API_BASE_URL", "https://bigship.test")
    monkeypatch.setattr(meta_module, "BIGSHIP_TRANSPORT", fake.transport())
    reset_db()
    with TestClient(create_app()) as c:
        assert c.app.state.meta is not None
        yield c


def _connect_whatsapp(client, headers):
    start = client.post("/api/meta/connect/whatsapp/start", headers=headers)
    assert start.status_code == 200, start.text
    res = client.post("/api/meta/connect/whatsapp/complete", headers=headers, json={
        "transaction_id": start.json()["transaction_id"], "code": "AQB-code-from-fb-login-1234",
        "waba_id": "1000000000000001", "phone_number_id": "2000000000000002", "event": "FINISH"})
    assert res.status_code == 200, res.text
    return res.json()


def test_spoofed_x_user_id_cannot_reach_another_sellers_connection(bigship_client):
    connection_id = _connect_whatsapp(bigship_client, A)["connections"][0]["connection_id"]
    # Seller B logs in with their own token but claims to be seller A.
    spoof = {**B, "X-User-Id": "1001"}
    assert bigship_client.get("/api/meta/connections", headers=spoof).json()["connections"] == []
    res = bigship_client.get(f"/api/meta/connections/{connection_id}", headers=spoof)
    assert res.status_code == 404
    assert bigship_client.delete(f"/api/meta/connections/{connection_id}", headers=spoof).status_code == 404
    # The owner still sees it.
    assert [c["connection_id"] for c in bigship_client.get("/api/meta/connections", headers=A).json()["connections"]] \
        == [connection_id]


def test_x_user_id_alone_is_not_a_login(bigship_client):
    res = bigship_client.get("/api/meta/connections", headers={"X-User-Id": "1001"})
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "META_SELLER_UNAUTHENTICATED"
    assert res.json()["detail"]["action"] == "login"


def test_platforms_report_the_verified_sellers_connections(bigship_client):
    anonymous = bigship_client.get("/api/meta/platforms").json()
    assert anonymous["seller_verified"] is False
    assert all(p["seller_connected"] is None and p["connection_status"] is None for p in anonymous["platforms"])

    _connect_whatsapp(bigship_client, A)
    mine = bigship_client.get("/api/meta/platforms", headers=A).json()
    assert mine["seller_verified"] is True
    assert {p["platform"]: (p["configured"], p["seller_connected"], p["connection_status"])
            for p in mine["platforms"]} == {
        "whatsapp": (True, True, "connected"),
        "instagram": (True, False, "not_connected"),
        "facebook": (True, False, "not_connected")}
    other = bigship_client.get("/api/meta/platforms", headers=B).json()
    assert all(p["seller_connected"] is False for p in other["platforms"])
    # An expired login does not break readiness; it just cannot show seller state.
    expired = bigship_client.get("/api/meta/platforms", headers={"X-Shipment-Authorization": "Bearer 9|old-token"})
    assert expired.status_code == 200 and expired.json()["seller_verified"] is False
