"""API contract, seller isolation, connection lifecycle, sync, webhooks and report context.

Runs against a disposable PostgreSQL (META_TEST_DATABASE_URL) and a fake Meta API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx

from app.modules.meta_integration.providers.graph import GraphError

from .conftest import (ADMIN_KEY, APP_SECRET, KEY1, KEY2, VERIFY_TOKEN, connect_redirect, connect_whatsapp, db_rows,
                       requires_db, run_worker, seller)
from .fake_meta import (IG_USER, META_USER, OTHER_PHONE, PAGE_NO_ANALYZE, PAGE_OK, PHONE, WABA, body_json, error)

pytestmark = requires_db
A, B = seller("seller-a"), seller("seller-b")


def _no_secrets(payload) -> None:
    text = json.dumps(payload)
    for marker in ("EAAbusinessToken", "EAApageToken", "IGlongLived", "access_token", "encrypted", "AUTHCODE"):
        assert marker not in text, marker


# ---- platforms / auth ---------------------------------------------------------------------------

def test_platforms_ready(client):
    body = client.get("/api/meta/platforms").json()
    assert {p["platform"]: p["configured"] for p in body["platforms"]} == {
        "whatsapp": True, "instagram": True, "facebook": True}
    assert body["webhook_configured"] is True


def test_seller_header_required(client):
    res = client.get("/api/meta/connections")
    assert res.status_code == 422 and res.json()["detail"]["code"] == "META_INVALID_REQUEST"


# ---- WhatsApp Embedded Signup ---------------------------------------------------------------------

def test_whatsapp_start_returns_only_safe_client_config(client):
    res = client.post("/api/meta/connect/whatsapp/start", headers=A).json()
    assert res["flow"] == "embedded_signup" and "authorization_url" not in res
    es = res["embedded_signup"]
    assert es["config_id"] == "wa-config" and es["login_options"]["response_type"] == "code"
    assert APP_SECRET not in json.dumps(res)


def test_whatsapp_connect_success(client, fake):
    res = connect_whatsapp(client)
    assert res.status_code == 200, res.text
    body = res.json()
    _no_secrets(body)
    conn = body["connections"][0]
    assert conn["platform"] == "whatsapp" and conn["connection_status"] == "connected"
    assert conn["external_account_id"] == WABA and "phone_registration_pending" in conn["notes"]
    # Code exchanged server-side (no redirect_uri), webhooks subscribed, token encrypted at rest.
    exchange = fake.called("GET", r"/oauth/access_token$")[0]
    assert exchange.params["code"] == "AQB-code-from-fb-login-1234" and "redirect_uri" not in exchange.params
    assert fake.called("POST", rf"/{WABA}/subscribed_apps")
    blob, version = db_rows("SELECT encrypted_access_token, key_version FROM meta_credentials")[0]
    assert b"EAAbusinessToken" not in bytes(blob) and version == "1"
    assets = {r[0]: r[1] for r in db_rows("SELECT asset_type, external_asset_id FROM meta_assets")}
    assert assets == {"whatsapp_business_account": WABA, "whatsapp_phone_number": PHONE}
    assert db_rows("SELECT job_type, status FROM meta_sync_jobs") == [("initial", "queued")]
    listed = client.get("/api/meta/connections", headers=A).json()["connections"]
    assert [c["connection_id"] for c in listed] == [conn["connection_id"]]
    _no_secrets(listed)


def test_whatsapp_pin_registers_number_and_is_not_stored(client, fake):
    res = connect_whatsapp(client, pin="123456")
    assert "phone_number_registered" in res.json()["connections"][0]["notes"]
    assert body_json(fake.called("POST", rf"/{PHONE}/register")[0]) == {"messaging_product": "whatsapp",
                                                                       "pin": "123456"}
    assert "123456" not in json.dumps(db_rows("SELECT details::text FROM meta_audit_logs"))


def test_whatsapp_rejects_waba_not_granted(client, fake):
    fake.granted_wabas = ["1000000000000999"]
    res = connect_whatsapp(client)
    assert res.status_code == 403 and res.json()["detail"]["code"] == "META_PERMISSION_DENIED"
    assert db_rows("SELECT count(*) FROM meta_connections")[0][0] == 0


def test_whatsapp_rejects_phone_from_other_waba(client):
    res = connect_whatsapp(client, phone_number_id=OTHER_PHONE)
    assert res.status_code == 403
    assert db_rows("SELECT count(*) FROM meta_credentials")[0][0] == 0


def test_whatsapp_cancelled_signup_rejected(client):
    res = connect_whatsapp(client, event="CANCEL")
    assert res.status_code == 400 and res.json()["detail"]["code"] == "META_AUTHORIZATION_FAILED"


def test_transaction_is_one_time_and_seller_bound(client):
    start = client.post("/api/meta/connect/whatsapp/start", headers=A).json()
    body = {"transaction_id": start["transaction_id"], "code": "AQB-code-from-fb-login-1234", "waba_id": WABA}
    stolen = client.post("/api/meta/connect/whatsapp/complete", headers=B, json=body)
    assert stolen.status_code == 400 and stolen.json()["detail"]["code"] == "META_TRANSACTION_INVALID"
    assert client.post("/api/meta/connect/whatsapp/complete", headers=A, json=body).status_code == 200
    replay = client.post("/api/meta/connect/whatsapp/complete", headers=A, json=body)
    assert replay.json()["detail"]["code"] == "META_TRANSACTION_INVALID"


def test_expired_transaction_rejected(client):
    start = client.post("/api/meta/connect/whatsapp/start", headers=A).json()
    db_rows("UPDATE meta_oauth_transactions SET expires_at = now() - interval '1 second' RETURNING id")
    res = client.post("/api/meta/connect/whatsapp/complete", headers=A, json={
        "transaction_id": start["transaction_id"], "code": "AQB-code-from-fb-login-1234", "waba_id": WABA})
    assert res.json()["detail"]["code"] == "META_TRANSACTION_INVALID"


def test_meta_failure_during_exchange_is_not_leaked(client, fake):
    fake.on("GET", "graph.facebook.com", r"/oauth/access_token", lambda r, m: error(100))
    res = connect_whatsapp(client)
    assert res.status_code == 400 and res.json()["detail"]["code"] == "META_AUTHORIZATION_FAILED"
    assert "TRACE123" not in res.text and "secret-free test error" not in res.text


# ---- duplicates / reconnect -----------------------------------------------------------------------

def test_reconnect_reuses_connection_and_other_seller_is_blocked(client):
    first = connect_whatsapp(client).json()["connections"][0]["connection_id"]
    db_rows("UPDATE meta_connections SET connection_status = 'expired' RETURNING id")
    again = connect_whatsapp(client).json()["connections"][0]
    assert again["connection_id"] == first and again["connection_status"] == "connected"
    other = connect_whatsapp(client, seller_id="seller-b")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "META_ACCOUNT_CONNECTED_ELSEWHERE"
    assert db_rows("SELECT count(*) FROM meta_connections")[0][0] == 1


# ---- Facebook / Instagram redirect flows ---------------------------------------------------------------

def test_facebook_flow(client, fake):
    res, start, cb = connect_redirect(client, "facebook")
    url = urlparse(start.json()["authorization_url"])
    q = parse_qs(url.query)
    assert url.netloc == "www.facebook.com" and url.path == "/v27.0/dialog/oauth"
    assert q["config_id"] == ["fb-config"] and q["redirect_uri"] == ["https://gateway.test/api/meta/oauth/callback"]
    # The callback never exposes the code; it returns the transaction ID to the Angular page.
    location = cb.headers["location"]
    assert location.startswith("https://panel.test/ai-box/create?") and "tab=sources" in location
    assert "meta_status=authorized" in location and "AUTHCODE" not in location
    body = res.json()
    assert res.status_code == 200, res.text
    _no_secrets(body)
    assert [c["external_account_id"] for c in body["connections"]] == [PAGE_OK]
    assert body["skipped"] == [{"page_id": PAGE_NO_ANALYZE, "name": "Other Page", "reason": "analyze_task_required"}]
    exchange = [c for c in fake.called("GET", r"/oauth/access_token$") if "code" in c.params][0]
    assert exchange.params["code"] == "AUTHCODE123"   # Meta's "#_" suffix removed


def test_facebook_pages_from_granular_scopes_when_me_accounts_is_empty(client, fake):
    # Facebook Login for Business: the picked Pages are on the token, not always in me/accounts.
    fake.on("GET", "graph.facebook.com", r"/me/accounts", {"data": []})
    fake.on("GET", "graph.facebook.com", rf"/{PAGE_OK}", lambda r, m: httpx.Response(200, json={
        "id": PAGE_OK, "name": "Seller Page", "category": "Shopping", "access_token": "EAApageToken000000000",
        "tasks": ["ANALYZE"]} if "access_token" in r.url.params.get("fields", "") else {
        "id": PAGE_OK, "name": "Seller Page", "category": "Shopping"}))
    fake.on("GET", "graph.facebook.com", rf"/{PAGE_NO_ANALYZE}", lambda r, m: error(10))
    res, _, _ = connect_redirect(client, "facebook")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [c["external_account_id"] for c in body["connections"]] == [PAGE_OK]
    assert body["skipped"] == [{"page_id": PAGE_NO_ANALYZE, "reason": "no_page_token"}]


def test_callback_with_unknown_state(client):
    res = client.get("/api/meta/oauth/callback", params={"state": "forged", "code": "x"}, follow_redirects=False)
    assert res.status_code == 302 and "meta_status=invalid" in res.headers["location"]
    assert "meta_transaction" not in res.headers["location"]


def test_callback_denied_by_user(client):
    start = client.post("/api/meta/connect/facebook/start", headers=A).json()
    state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
    res = client.get("/api/meta/oauth/callback", params={"state": state, "error": "access_denied"},
                     follow_redirects=False)
    assert "meta_status=denied" in res.headers["location"]
    complete = client.post("/api/meta/connect/facebook/complete", headers=A,
                           json={"transaction_id": start["transaction_id"]})
    assert complete.json()["detail"]["code"] == "META_TRANSACTION_INVALID"


def test_complete_before_callback_rejected(client):
    start = client.post("/api/meta/connect/instagram/start", headers=A).json()
    res = client.post("/api/meta/connect/instagram/complete", headers=A,
                      json={"transaction_id": start["transaction_id"]})
    assert res.json()["detail"]["code"] == "META_TRANSACTION_INVALID"


def test_instagram_flow_and_eligibility(client, fake):
    res, start, _ = connect_redirect(client, "instagram")
    q = parse_qs(urlparse(start.json()["authorization_url"]).query)
    assert q["scope"] == ["instagram_business_basic,instagram_business_manage_insights"]
    assert res.status_code == 200, res.text
    conn = res.json()["connections"][0]
    assert conn["external_account_id"] == IG_USER and conn["account_name"] == "seller.shop"
    fake.ig_account_type = "PERSONAL"
    res2, _, _ = connect_redirect(client, "instagram", seller_id="seller-b")
    assert res2.status_code == 422 and res2.json()["detail"]["code"] == "META_ACCOUNT_NOT_ELIGIBLE"


def _ig_facebook_login(fake, scopes=("instagram_basic", "instagram_manage_insights", "pages_show_list"),
                       me_accounts=True):
    """Fake Meta for Instagram through Facebook Login for Business (Unified login)."""
    fb = "graph.facebook.com"
    fake.on("GET", fb, r"/debug_token", {"data": {
        "app_id": "111", "is_valid": True, "expires_at": 0, "scopes": list(scopes), "user_id": META_USER,
        "granular_scopes": [{"scope": "instagram_basic", "target_ids": [IG_USER]},
                            {"scope": "pages_show_list", "target_ids": [PAGE_OK]}]}})
    page = {"id": PAGE_OK, "name": "Seller Page", "access_token": "EAApageToken000000000",
            "instagram_business_account": {"id": IG_USER, "username": "seller.shop"}}
    fake.on("GET", fb, r"/me/accounts", {"data": [page] if me_accounts else []})
    fake.on("GET", fb, rf"/{PAGE_OK}", page)
    fake.on("GET", fb, rf"/{IG_USER}", {"id": IG_USER, "username": "seller.shop", "followers_count": 1500,
                                        "media_count": 42})
    fake.on("GET", fb, rf"/{IG_USER}/insights", {"data": [{"name": "views", "total_value": {"value": 4}}]})
    fake.on("GET", fb, rf"/{IG_USER}/media", {"data": []})


def _ig_fb_client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    from .conftest import reset_db
    monkeypatch.setenv("META_INSTAGRAM_CONFIG_ID", "ig-config")
    monkeypatch.setenv("META_INSTAGRAM_APP_ID", "")
    monkeypatch.setenv("META_INSTAGRAM_APP_SECRET", "")
    reset_db()
    return TestClient(create_app())


def test_instagram_through_facebook_login(meta_env, monkeypatch, fake):
    _ig_facebook_login(fake, me_accounts=False)   # Business Login: Pages only on the token
    with _ig_fb_client(monkeypatch) as client:
        platforms = client.get("/api/meta/platforms").json()["platforms"]
        assert {p["platform"]: p["configured"] for p in platforms}["instagram"] is True
        res, start, _ = connect_redirect(client, "instagram")
        q = parse_qs(urlparse(start.json()["authorization_url"]).query)
        assert urlparse(start.json()["authorization_url"]).netloc == "www.facebook.com"
        assert q["config_id"] == ["ig-config"] and q["client_id"] == ["111"]
        assert res.status_code == 200, res.text
        _no_secrets(res.json())
        conn = res.json()["connections"][0]
        assert conn["external_account_id"] == IG_USER and conn["account_name"] == "seller.shop"
        # The linked Page's token (never expires) is stored, not the user token.
        assert db_rows("SELECT expires_at FROM meta_credentials") == [(None,)]
        run_worker(client)
        assert db_rows("SELECT status FROM meta_sync_jobs") == [("succeeded",)]
        assert fake.called("GET", rf"/{IG_USER}/insights") and not [
            c for c in fake.calls if c.host == "graph.instagram.com"]


def test_instagram_facebook_login_missing_permission(meta_env, monkeypatch, fake):
    _ig_facebook_login(fake, scopes=("instagram_basic", "pages_show_list"))
    with _ig_fb_client(monkeypatch) as client:
        res, _, _ = connect_redirect(client, "instagram")
        assert res.status_code == 403, res.text
        assert res.json()["detail"]["code"] == "META_PERMISSION_DENIED"


# ---- seller isolation ------------------------------------------------------------------------------

def test_seller_isolation(client):
    conn_id = connect_whatsapp(client).json()["connections"][0]["connection_id"]
    job_id = db_rows("SELECT id::text FROM meta_sync_jobs")[0][0]
    assert client.get("/api/meta/connections", headers=B).json() == {"connections": []}
    for method, path in (("GET", f"/api/meta/connections/{conn_id}"), ("DELETE", f"/api/meta/connections/{conn_id}"),
                         ("POST", f"/api/meta/connections/{conn_id}/sync"), ("GET", f"/api/meta/sync-jobs/{job_id}")):
        res = client.request(method, path, headers=B)
        assert res.status_code == 404, (method, path, res.text)
    assert db_rows("SELECT connection_status FROM meta_connections")[0][0] == "connected"
    report = client.post("/api/meta/report-context", headers=B, json={
        "data_sources": ["whatsapp"], "from_date": "2025-11-01", "to_date": "2025-11-30"}).json()
    assert report["sources"]["whatsapp"]["status"] == "not_connected"
    assert db_rows("SELECT count(*) FROM meta_audit_logs WHERE action = 'access_denied' AND seller_id = 'seller-b'"
                   )[0][0] >= 3


# ---- sync ------------------------------------------------------------------------------------------

def test_initial_sync_and_idempotent_upserts(client):
    conn_id = connect_whatsapp(client).json()["connections"][0]["connection_id"]
    run_worker(client)
    assert db_rows("SELECT status, records_processed FROM meta_sync_jobs")[0][0] == "succeeded"
    count = db_rows("SELECT count(*) FROM meta_insights")[0][0]
    assert count > 0
    job = client.post(f"/api/meta/connections/{conn_id}/sync", headers=A)
    assert job.status_code == 202 and job.json()["status"] == "queued"
    again = client.post(f"/api/meta/connections/{conn_id}/sync", headers=A)
    assert again.json()["job_id"] == job.json()["job_id"] and again.headers["x-meta-sync-existing"] == "true"
    run_worker(client)
    assert db_rows("SELECT count(*) FROM meta_insights")[0][0] == count   # upserted, not duplicated
    status = client.get(f"/api/meta/sync-jobs/{job.json()['job_id']}", headers=A).json()
    assert status["status"] == "succeeded" and status["records_processed"] > 0 and status["error_code"] is None
    detail = client.get(f"/api/meta/connections/{conn_id}", headers=A).json()
    assert detail["last_successful_sync_at"] and not detail["data_stale"]
    reasons = {m["metric_name"]: m["availability_status"] for m in detail["metrics"]}
    assert reasons["template_sent"] == "permission_required"
    _no_secrets(detail)


def test_token_expiry_marks_reconnect(client, fake):
    conn_id = connect_whatsapp(client).json()["connections"][0]["connection_id"]
    fake.on("GET", "graph.facebook.com", rf"/{WABA}", lambda r, m: error(190, subcode=463))
    run_worker(client)
    row = db_rows("SELECT connection_status, last_error_code FROM meta_connections")[0]
    assert row == ("expired", "META_TOKEN_EXPIRED")
    assert db_rows("SELECT status, last_error FROM meta_sync_jobs")[0] == ("failed", "META_TOKEN_EXPIRED")
    summary = client.get("/api/meta/connections", headers=A).json()["connections"][0]
    assert summary["reconnect_required"] is True
    manual = client.post(f"/api/meta/connections/{conn_id}/sync", headers=A)
    assert manual.status_code == 409 and manual.json()["detail"]["code"] == "META_RECONNECT_REQUIRED"
    assert db_rows("SELECT count(*) FROM meta_audit_logs WHERE action = 'token_invalidated'")[0][0] == 1


def test_rate_limit_requeues_with_backoff(client, fake):
    connect_whatsapp(client)
    fake.on("GET", "graph.facebook.com", rf"/{WABA}", lambda r, m: error(80008))
    run_worker(client)
    status, retries, err, delay = db_rows(
        "SELECT status, retry_count, last_error, extract(epoch FROM run_after - now()) FROM meta_sync_jobs")[0]
    assert (status, retries, err) == ("queued", 1, "META_RATE_LIMITED") and delay > 30
    assert db_rows("SELECT connection_status FROM meta_connections")[0][0] == "connected"


def test_provider_crash_retries_then_fails(client, fake):
    connect_whatsapp(client)
    fake.on("GET", "graph.facebook.com", rf"/{WABA}", lambda r, m: httpx.Response(500, json={}))
    for _ in range(7):
        db_rows("UPDATE meta_sync_jobs SET run_after = now() WHERE status = 'queued' RETURNING id")
        run_worker(client)
    assert db_rows("SELECT status, retry_count, last_error FROM meta_sync_jobs")[0] == ("failed", 5, "META_SYNC_FAILED")


def test_expired_lease_is_recovered(client):
    connect_whatsapp(client)
    db_rows("UPDATE meta_sync_jobs SET status = 'running', lease_until = now() - interval '1 minute' RETURNING id")
    run_worker(client)
    assert db_rows("SELECT status, retry_count FROM meta_sync_jobs")[0] == ("succeeded", 1)


def test_instagram_token_refresh_job(client, fake):
    connect_redirect(client, "instagram")
    run_worker(client)   # initial sync
    db_rows("UPDATE meta_credentials SET expires_at = now() + interval '3 days', issued_at = now() - interval '2 days'"
            " RETURNING connection_id")
    run_worker(client)
    assert ("token_refresh", "succeeded") in db_rows("SELECT job_type, status FROM meta_sync_jobs")
    assert fake.called("GET", r"/refresh_access_token")
    assert db_rows("SELECT expires_at > now() + interval '50 days' FROM meta_credentials")[0][0]


# ---- disconnect --------------------------------------------------------------------------------------

def test_disconnect_revokes_and_deletes_data(client, fake):
    conn_id = connect_whatsapp(client).json()["connections"][0]["connection_id"]
    run_worker(client)
    client.post(f"/api/meta/connections/{conn_id}/sync", headers=A)
    res = client.delete(f"/api/meta/connections/{conn_id}", headers=A, params={"delete_data": "true"})
    assert res.status_code == 200 and res.json()["connection_status"] == "disconnected"
    assert fake.called("DELETE", rf"/{WABA}/subscribed_apps")
    assert db_rows("SELECT encrypted_access_token IS NULL, revoked_at IS NOT NULL FROM meta_credentials")[0] == (
        True, True)
    assert db_rows("SELECT count(*) FROM meta_insights")[0][0] == 0
    assert ("manual", "cancelled") in db_rows("SELECT job_type, status FROM meta_sync_jobs")
    assert client.get("/api/meta/connections", headers=A).json() == {"connections": []}


def test_disconnect_keeps_history_by_default(client):
    conn_id = connect_whatsapp(client).json()["connections"][0]["connection_id"]
    run_worker(client)
    client.delete(f"/api/meta/connections/{conn_id}", headers=A)
    assert db_rows("SELECT count(*) FROM meta_insights")[0][0] > 0


# ---- webhooks -------------------------------------------------------------------------------------------

def _signed_post(client, body: dict, secret: str = APP_SECRET):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/meta/webhook", content=raw, headers={"X-Hub-Signature-256": sig,
                                                                 "Content-Type": "application/json"})


def _status_body(phone: str, wamid: str = "wamid.1", status: str = "read") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "time": 1, "changes": [{
        "field": "messages", "value": {"metadata": {"phone_number_id": phone}, "statuses": [
            {"id": wamid, "status": status, "timestamp": "1762905700", "recipient_id": "919999999999"}]}}]}]}


def test_webhook_verification(client):
    ok = client.get("/api/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                                 "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get("/api/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                  "hub.challenge": "x"})
    assert bad.status_code == 403


def test_webhook_signature_required(client):
    res = client.post("/api/meta/webhook", content=b'{"object":"page"}',
                      headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
    assert res.status_code == 401
    assert _signed_post(client, {"object": "page"}, secret="wrong").status_code == 401
    assert db_rows("SELECT count(*) FROM meta_webhook_events")[0][0] == 0


def test_webhook_dedupe_and_attribution(client):
    connect_whatsapp(client)
    first = _signed_post(client, _status_body(PHONE))
    assert first.json() == {"received": 1, "stored": 1}
    assert _signed_post(client, _status_body(PHONE)).json() == {"received": 1, "stored": 0}   # Meta retry
    _signed_post(client, _status_body(OTHER_PHONE, "wamid.2"))   # phone not in the seller's WABA
    run_worker(client)
    states = dict(db_rows("SELECT event_type || ':' || (payload->>'phone_number_id'), processing_status "
                          "FROM meta_webhook_events"))
    assert states == {f"messages:{PHONE}": "processed", f"messages:{OTHER_PHONE}": "unmatched"}
    rows = db_rows("SELECT seller_id, status, message_ref FROM meta_whatsapp_status_events")
    assert len(rows) == 1 and rows[0][:2] == ("seller-a", "read") and rows[0][2] != "wamid.1"
    raw = json.dumps(db_rows("SELECT payload::text FROM meta_webhook_events"))
    assert "919999999999" not in raw and "wamid.1" not in raw


def test_partner_removed_webhook_requires_reconnect(client):
    connect_whatsapp(client)
    _signed_post(client, {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [
        {"field": "account_update", "value": {"event": "PARTNER_REMOVED"}}]}]})
    run_worker(client)
    assert db_rows("SELECT connection_status FROM meta_connections")[0][0] == "reconnect_required"


# ---- deauthorize / data deletion -----------------------------------------------------------------------

def _signed_request(user_id: str, secret: str = APP_SECRET) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"algorithm": "HMAC-SHA256", "user_id": user_id}).encode()
                                       ).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=")
    return f"{sig.decode()}.{payload}"


def test_data_deletion_callback(client):
    connect_whatsapp(client)
    run_worker(client)
    assert client.post("/api/meta/data-deletion", data={"signed_request": _signed_request(META_USER, "x")}
                       ).status_code == 400
    res = client.post("/api/meta/data-deletion", data={"signed_request": _signed_request(META_USER)})
    assert res.status_code == 200
    body = res.json()
    assert body["url"] == f"https://gateway.test/api/meta/data-deletion/{body['confirmation_code']}"
    assert db_rows("SELECT connection_status FROM meta_connections")[0][0] == "disconnected"
    assert db_rows("SELECT count(*) FROM meta_insights")[0][0] == 0
    status = client.get(f"/api/meta/data-deletion/{body['confirmation_code']}")
    assert status.json()["status"] == "completed"


# ---- reporting --------------------------------------------------------------------------------------------

def _report(client, sources, headers=A, start="2025-11-01", end="2025-11-30"):
    res = client.post("/api/meta/report-context", headers=headers,
                      json={"data_sources": sources, "from_date": start, "to_date": end})
    assert res.status_code == 200, res.text
    return res.json()


def test_report_context_whatsapp(client):
    connect_whatsapp(client)
    run_worker(client)
    _signed_post(client, _status_body(PHONE))
    run_worker(client)
    report = _report(client, ["whatsapp"])
    source = report["sources"]["whatsapp"]
    assert source["status"] == "ok" and source["caveats"]
    account = source["accounts"][0]
    sent = account["metrics"]["messages_sent"]
    assert sent["total"] == 200.0 and sent["additive"] and [d["value"] for d in sent["daily"]] == [120.0, 80.0]
    assert {d["dimension"]: d["total"] for d in account["metrics"]["pricing_volume"]["by_dimension"]} == {
        "MARKETING": 40.0, "UTILITY": 80.0}
    assert account["metrics"]["status_read"]["total"] == 1.0
    assert account["metrics"]["status_read"]["source"] == "webhook"
    unavailable = {u["metric"]: u["reason"] for u in account["unavailable_metrics"]}
    assert unavailable["template_read"] == "permission_required"
    assert account["coverage"]["complete"] is False and account["coverage"]["days_with_data"] == 2
    assert account["last_successful_sync_at"] and report["comparison_rules"]
    _no_secrets(report)


def test_report_context_multi_source_and_not_connected(client):
    connect_redirect(client, "facebook")
    run_worker(client)
    report = _report(client, ["shipments", "facebook", "instagram"], start="2026-11-01", end="2026-11-16")
    assert report["sources"]["shipments"]["status"] == "external"
    assert report["sources"]["instagram"]["status"] == "not_connected"
    assert report["sources"]["instagram"]["error"]["code"] == "META_NOT_CONNECTED"
    fb = report["sources"]["facebook"]["accounts"][0]
    assert fb["metrics"]["page_media_view"]["total"] == 30.0
    unique = fb["metrics"]["page_total_media_view_unique"]
    assert not unique["additive"] and "total" not in unique and unique["latest"] == 20.0
    assert fb["items"]["post_reactions"]["items"][0]["by_reaction"] == {"like": 12.0, "love": 3.0}
    assert {u["metric"]: u["reason"] for u in fb["unavailable_metrics"]}["page_views_total"] == "unsupported"


def test_report_context_validation(client):
    res = client.post("/api/meta/report-context", headers=A, json={
        "data_sources": ["tiktok"], "from_date": "2025-11-01", "to_date": "2025-11-30"})
    assert res.status_code == 422
    res = client.post("/api/meta/report-context", headers=A, json={
        "data_sources": ["whatsapp"], "from_date": "2025-11-30", "to_date": "2025-11-01"})
    assert res.status_code == 422


def test_report_marks_expired_connection(client, fake):
    connect_whatsapp(client)
    fake.on("GET", "graph.facebook.com", rf"/{WABA}", lambda r, m: error(190, subcode=463))
    run_worker(client)
    source = _report(client, ["whatsapp"])["sources"]["whatsapp"]
    assert source["status"] == "reconnect_required"
    assert {u["reason"] for u in source["accounts"][0]["unavailable_metrics"]} == {"expired_token"}


# ---- admin --------------------------------------------------------------------------------------------------

def test_admin_requires_key_and_rotates(client, monkeypatch):
    connect_whatsapp(client)
    assert client.get("/api/meta/admin/connections").status_code == 403
    listed = client.get("/api/meta/admin/connections", headers={"X-Meta-Admin-Key": ADMIN_KEY}).json()
    assert listed["connections"][0]["seller_id"] == "seller-a"
    _no_secrets(listed)
    # Rotate: add key 2 as active, re-encrypt, then the old key can be removed.
    from app.modules.meta_integration.security import TokenCipher
    ctx = client.app.state.meta.ctx
    ctx.cipher = TokenCipher({"1": base64.b64decode(KEY1), "2": base64.b64decode(KEY2)}, "2")
    res = client.post("/api/meta/admin/rotate-keys", headers={"X-Meta-Admin-Key": ADMIN_KEY})
    assert res.json() == {"rotated": 1, "active_key_version": "2"}
    ctx.cipher = TokenCipher({"2": base64.b64decode(KEY2)}, "2")
    run_worker(client)   # sync decrypts with key 2 only
    assert db_rows("SELECT status FROM meta_sync_jobs")[0][0] == "succeeded"


def test_graph_error_type_is_exception():
    assert issubclass(GraphError, Exception)
