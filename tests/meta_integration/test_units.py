"""Database-free tests: security primitives, error mapping, providers, normalization, webhooks."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.modules.meta_integration.config import MetaSettings
from app.modules.meta_integration.providers import FacebookProvider, GraphClient, InstagramProvider, WhatsAppProvider
from app.modules.meta_integration.providers.base import SyncWindow, parse_time
from app.modules.meta_integration.providers.graph import GraphError, classify
from app.modules.meta_integration.security import (TokenCipher, parse_signed_request, redact,
                                                    verify_webhook_signature)
from app.modules.meta_integration.services.webhooks import extract_events

from .conftest import META_ENV
from .fake_meta import IG_APP_SCOPED, IG_USER, PAGE_OK, WABA, FakeMeta, error

K1, K2 = b"a" * 32, b"b" * 32


# ---- encryption ------------------------------------------------------------------------------------

def test_token_encryption_roundtrip_and_binding():
    cipher = TokenCipher({"1": K1}, "1")
    blob, version = cipher.encrypt("EAAsecret", "conn-1")
    assert b"EAAsecret" not in blob and version == "1"
    assert cipher.decrypt(blob, version, "conn-1") == "EAAsecret"
    with pytest.raises(ValueError):   # copied to another row -> integrity failure
        cipher.decrypt(blob, version, "conn-2")


def test_key_rotation_old_versions_still_decrypt():
    old = TokenCipher({"1": K1}, "1")
    blob, version = old.encrypt("tok", "c")
    new = TokenCipher({"1": K1, "2": K2}, "2")
    assert new.decrypt(blob, version, "c") == "tok"
    assert new.encrypt("tok", "c")[1] == "2"
    with pytest.raises(KeyError):
        TokenCipher({"2": K2}, "2").decrypt(blob, version, "c")


# ---- signatures ------------------------------------------------------------------------------------

def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature():
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_webhook_signature(body, _sign(body, "s1"), ["s1"])
    assert verify_webhook_signature(body, _sign(body, "s2"), ["s1", "s2"])   # Instagram app secret
    assert not verify_webhook_signature(body + b" ", _sign(body, "s1"), ["s1"])
    assert not verify_webhook_signature(body, None, ["s1"])
    assert not verify_webhook_signature(body, "sha1=abc", ["s1"])
    assert not verify_webhook_signature(body, _sign(body, "s1"), [""])


def test_signed_request():
    payload = base64.urlsafe_b64encode(json.dumps({"algorithm": "HMAC-SHA256", "user_id": "42"}).encode()
                                       ).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(b"secret", payload.encode(), hashlib.sha256).digest()).rstrip(b"=")
    good = f"{sig.decode()}.{payload}"
    assert parse_signed_request(good, "secret")["user_id"] == "42"
    assert parse_signed_request(good, "other") is None
    assert parse_signed_request("garbage", "secret") is None


def test_redaction():
    out = redact({"access_token": "EAAxyz", "code": "abc", "client_secret": "s", "page": "ok",
                  "nested": {"text": "hello customer"}, "note": "token EAA" + "x" * 40})
    assert out["access_token"] == out["code"] == out["client_secret"] == "[REDACTED]"
    assert out["nested"]["text"] == "[REDACTED]" and out["page"] == "ok"
    assert "EAAxxxx" not in out["note"]


# ---- error mapping ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status,code,subcode,kind", [
    (400, 190, 463, "token_expired"), (400, 190, 460, "token_invalid"), (400, 4, None, "rate_limited"),
    (400, 80004, None, "rate_limited"), (429, None, None, "rate_limited"), (403, 10, None, "permission"),
    (400, 200, None, "permission"), (400, 100, None, "invalid_param"), (500, 1, None, "server")])
def test_classify(status, code, subcode, kind):
    payload = {"error": {"code": code, "error_subcode": subcode}} if code else {}
    assert classify(status, payload).kind == kind


def test_rate_limit_retry_after_from_usage_header():
    headers = httpx.Headers({"x-business-use-case-usage": json.dumps(
        {"123": [{"type": "whatsapp", "estimated_time_to_regain_access": 7}]})})
    assert classify(400, {"error": {"code": 80008}}, headers).retry_after == 420


def test_parse_time_formats():
    assert parse_time("2026-11-13T08:00:00+0000") == datetime(2026, 11, 13, 8, tzinfo=timezone.utc)
    assert parse_time(1762905600) == datetime(2025, 11, 12, tzinfo=timezone.utc)
    assert parse_time(None) is None and parse_time("nope") is None


# ---- providers (mocked Meta) -----------------------------------------------------------------------

def _settings(monkeypatch) -> MetaSettings:
    for key, value in META_ENV.items():
        monkeypatch.setenv(key, value)
    return MetaSettings()


def _run(coro):
    return asyncio.run(coro)


def _graph(fake: FakeMeta) -> GraphClient:
    return GraphClient("v27.0", "test-app-secret", transport=fake.transport())


WINDOW = SyncWindow(datetime(2025, 11, 12, tzinfo=timezone.utc), datetime(2025, 11, 14, tzinfo=timezone.utc))


def test_whatsapp_insights_normalized_and_template_reason(monkeypatch):
    fake = FakeMeta()
    provider = WhatsAppProvider(_graph(fake), _settings(monkeypatch))
    result = _run(provider.fetch_insights("EAAbiz", WABA, [], WINDOW))
    sent = [p for p in result.points if p.metric_name == "messages_sent"]
    assert [p.value for p in sent] == [120.0, 80.0] and sent[0].unit == "messages"
    volume = {p.dimensions["pricing_category"]: p.value for p in result.points if p.metric_name == "pricing_volume"}
    assert volume == {"MARKETING": 40.0, "UTILITY": 80.0}
    statuses = {s.metric_name: s.status for s in result.statuses}
    assert statuses["messages_sent"] == "available"
    assert statuses["template_read"] == "permission_required"   # never auto-enabled
    assert not fake.called("POST", rf"/{WABA}$")                  # is_enabled_for_insights never sent
    # Authorization: token in header with appsecret_proof, never in the query string.
    call = fake.called("GET", rf"/{WABA}$")[0]
    assert call.headers["authorization"].startswith("Bearer ") and "appsecret_proof" in call.params
    assert "access_token" not in call.params


def test_instagram_unsupported_metric_is_isolated(monkeypatch):
    fake = FakeMeta()
    provider = InstagramProvider(_graph(fake), _settings(monkeypatch))
    from app.modules.meta_integration.providers.base import AssetInfo
    asset = AssetInfo("instagram_account", IG_USER, "seller.shop", {"api_user_id": IG_APP_SCOPED})
    result = _run(provider.fetch_insights("IGtoken", IG_USER, [asset], WINDOW))
    names = {p.metric_name for p in result.points}
    assert {"views", "likes", "reach", "follows", "unfollows", "followers_count"} <= names
    assert "replies" not in names
    statuses = {s.metric_name: (s.status, s.reason) for s in result.statuses}
    assert statuses["replies"][0] == "unsupported"
    # No media published in the window: reported as not available, no fabricated values.
    assert statuses["media_views"][0] == "not_available"
    assert not [p for p in result.points if p.metric_name.startswith("media_") and p.metric_name != "media_count"]
    assert [p.value for p in result.points if p.metric_name == "follows"] == [5.0, 5.0]


def test_tokens_never_reach_logs(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    fake = FakeMeta()
    provider = InstagramProvider(_graph(fake), _settings(monkeypatch))
    _run(provider.exchange_code("SECRETCODE123#_"))
    _run(WhatsAppProvider(_graph(fake), _settings(monkeypatch)).exchange_code("WACODE999"))
    text = caplog.text
    assert "IGlongLived" not in text and "IGshortLived" not in text and "SECRETCODE123" not in text
    assert "WACODE999" not in text and "test-app-secret" not in text and "test-ig-secret" not in text
    assert "[REDACTED]" in text


def test_instagram_empty_data_is_not_zero(monkeypatch):
    fake = FakeMeta()
    fake.on("GET", "graph.instagram.com", rf"/{IG_APP_SCOPED}/insights", {"data": []})
    provider = InstagramProvider(_graph(fake), _settings(monkeypatch))
    from app.modules.meta_integration.providers.base import AssetInfo
    asset = AssetInfo("instagram_account", IG_USER, None, {"api_user_id": IG_APP_SCOPED})
    result = _run(provider.fetch_insights("IGtoken", IG_USER, [asset], WINDOW))
    assert not [p for p in result.points if p.metric_name in {"views", "reach", "likes"}]


def test_facebook_rejected_metric_falls_back_per_metric(monkeypatch):
    fake = FakeMeta()
    provider = FacebookProvider(_graph(fake), _settings(monkeypatch))
    result = _run(provider.fetch_insights("EAApage", PAGE_OK, [], WINDOW))
    names = {p.metric_name for p in result.points}
    assert "page_media_view" in names and "page_views_total" not in names
    statuses = {s.metric_name: s.status for s in result.statuses}
    assert statuses["page_views_total"] == "unsupported" and statuses["page_media_view"] == "available"
    reactions = {p.dimensions["reaction_type"]: p.value for p in result.points if p.metric_name == "post_reactions"}
    assert reactions == {"like": 12.0, "love": 3.0}
    day = next(p for p in result.points if p.metric_name == "page_media_view")
    assert day.period_start == datetime(2026, 11, 11, 8, tzinfo=timezone.utc)
    # Retired metrics are never requested.
    requested = ",".join(c.params.get("metric", "") for c in fake.calls)
    assert "page_impressions" not in requested and "page_fans" not in requested


def test_rate_limit_propagates(monkeypatch):
    fake = FakeMeta()
    fake.on("GET", "graph.facebook.com", rf"/{WABA}", lambda r, m: error(80008))
    provider = WhatsAppProvider(_graph(fake), _settings(monkeypatch))
    with pytest.raises(GraphError) as exc:
        _run(provider.fetch_insights("EAAbiz", WABA, [], WINDOW))
    assert exc.value.kind == "rate_limited"


def test_platform_config_requires_https_in_production(monkeypatch):
    settings = _settings(monkeypatch)
    assert settings.platform_problems("whatsapp") == ["DATABASE_URL"]
    monkeypatch.setenv("META_OAUTH_REDIRECT_URI", "http://gateway.test/cb")
    from app.core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("ENV", "production")
    try:
        problems = MetaSettings().platform_problems("facebook")
        assert "META_OAUTH_REDIRECT_URI (https required)" in problems
        # A browser-supplied seller ID is never accepted in production.
        assert "META_SELLER_IDENTITY (header is not allowed in production)" in problems
    finally:
        monkeypatch.delenv("ENV")
        get_settings.cache_clear()


# ---- webhooks: minimization + dedupe ids ---------------------------------------------------------

WA_BODY = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "messages", "value": {
    "messaging_product": "whatsapp", "metadata": {"display_phone_number": "919876543210", "phone_number_id": "2000"},
    "contacts": [{"profile": {"name": "Customer Name"}, "wa_id": "919999999999"}],
    "messages": [{"from": "919999999999", "id": "wamid.IN", "type": "text", "text": {"body": "my order is late"}}],
    "statuses": [{"id": "wamid.OUT", "status": "read", "timestamp": "1762905700", "recipient_id": "919999999999",
                  "pricing": {"category": "utility"}}]}}]}]}


def test_webhook_events_are_minimized():
    events = extract_events(WA_BODY)
    assert len(events) == 1
    stored = json.dumps(events[0]["payload"])
    for secret in ("my order is late", "919999999999", "Customer Name", "wamid.OUT"):
        assert secret not in stored
    assert events[0]["payload"]["inbound_messages"] == 1
    assert events[0]["payload"]["statuses"][0]["status"] == "read"
    assert extract_events(WA_BODY)[0]["event_id"] == events[0]["event_id"]   # stable -> dedupe
    assert extract_events({"object": "unknown"}) == []
