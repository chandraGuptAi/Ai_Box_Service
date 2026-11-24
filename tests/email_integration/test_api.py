"""End-to-end /api/email tests against a disposable PostgreSQL and fake Google/Microsoft APIs."""
from __future__ import annotations

import json
from email import message_from_bytes
from email.policy import default as default_policy
from urllib.parse import parse_qs, urlparse

from .conftest import PDF, connect, db_rows, internal, requires_db, seller
from .fake_providers import UPLOAD_URL

pytestmark = requires_db


def draft(client, connection_id, pdf=PDF, headers=None, **overrides):
    metadata = {"connection_id": connection_id, "subject": "Weekly shipment report", "html": "<p>Hello</p>",
                "text": "Hello", "report_id": "rep-1", "user_id": "u-1"} | overrides
    files = {"attachment": ("Shipment Report.pdf", pdf, "application/pdf")} if pdf is not None else None
    return client.post("/api/email/internal/draft", data={"metadata": json.dumps(metadata)}, files=files,
                       headers=headers or internal())


# ---- connect --------------------------------------------------------------------------------------

def test_providers_report_readiness_and_my_mailboxes(client):
    body = client.get("/api/email/providers", headers=seller()).json()
    assert body["enabled"] is True
    assert {p["provider"]: p["configured"] for p in body["providers"]} == {"gmail": True, "outlook": True}
    assert body["connections"] == []
    connect(client)
    assert [c["account_email"] for c in client.get("/api/email/providers", headers=seller()).json()["connections"]] \
        == ["seller@gmail.com"]


def test_gmail_connect_asks_for_compose_with_pkce_and_stores_only_an_encrypted_refresh_token(client, fake):
    start = client.post("/api/email/connect/gmail/start", headers=seller()).json()
    query = parse_qs(urlparse(start["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["access_type"] == ["offline"]
    assert query["scope"] == ["openid email https://www.googleapis.com/auth/gmail.compose"]

    result = connect(client)
    assert result["_status"] == 200
    assert result["provider"] == "gmail" and result["account_email"] == "seller@gmail.com"
    assert fake.token_requests[-1]["code_verifier"][0]
    blob = db_rows("SELECT encrypted_refresh_token FROM email_credentials")[0][0]
    assert b"g-refresh" not in bytes(blob)
    assert all(len(s[0]) == 64 for s in db_rows("SELECT state_hash FROM email_oauth_transactions"))


def test_outlook_connect_asks_for_mail_readwrite(client):
    start = client.post("/api/email/connect/outlook/start", headers=seller()).json()
    scope = parse_qs(urlparse(start["authorization_url"]).query)["scope"][0]
    assert scope == "openid email offline_access User.Read Mail.ReadWrite"


def test_a_transaction_is_single_use_and_bound_to_its_seller(client):
    start = client.post("/api/email/connect/gmail/start", headers=seller("S1")).json()
    state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
    client.get("/api/email/oauth/callback", params={"state": state, "code": "good-code"})
    other = client.post("/api/email/connect/gmail/complete", json={"transaction_id": start["transaction_id"]},
                        headers=seller("S2"))
    assert other.status_code == 400 and other.json()["detail"]["code"] == "EMAIL_TRANSACTION_INVALID"
    ok = client.post("/api/email/connect/gmail/complete", json={"transaction_id": start["transaction_id"]},
                     headers=seller("S1"))
    assert ok.status_code == 200
    again = client.post("/api/email/connect/gmail/complete", json={"transaction_id": start["transaction_id"]},
                        headers=seller("S1"))
    assert again.json()["detail"]["code"] == "EMAIL_TRANSACTION_INVALID"
    assert client.get("/api/email/connections", headers=seller("S2")).json()["connections"] == []


def test_denied_and_unknown_callbacks(client):
    start = client.post("/api/email/connect/outlook/start", headers=seller()).json()
    state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
    page = client.get("/api/email/oauth/callback", params={"state": state, "error": "access_denied"})
    assert "not granted" in page.text
    res = client.post("/api/email/connect/outlook/complete", json={"transaction_id": start["transaction_id"]},
                      headers=seller())
    assert res.json()["detail"]["code"] == "EMAIL_AUTHORIZATION_DENIED"
    assert "expired" in client.get("/api/email/oauth/callback", params={"state": "nope", "code": "x"}).text


def test_the_callback_page_stays_open_for_the_draft(client):
    start = client.post("/api/email/connect/gmail/start", headers=seller()).json()
    state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
    page = client.get("/api/email/oauth/callback", params={"state": state, "code": "good-code"})
    assert "Signed in to Gmail" in page.text and "window.close" not in page.text


def test_missing_compose_permission_is_refused(client):
    result = connect(client, code="no-compose")
    assert result["_status"] == 403 and result["detail"]["code"] == "EMAIL_PERMISSION_MISSING"
    assert db_rows("SELECT count(*) FROM email_connections")[0][0] == 0


# ---- drafts ---------------------------------------------------------------------------------------

def test_gmail_draft_has_the_body_and_pdf_and_opens_in_the_right_account(client, fake):
    conn = connect(client)["connection_id"]
    res = draft(client, conn)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_id"] == "r-1" and body["from_email"] == "seller@gmail.com"
    assert body["open_url"] == "https://mail.google.com/mail/?authuser=seller%40gmail.com#drafts?compose=18c0abc1"
    assert body["attachment_name"] == "Shipment Report.pdf"

    msg = message_from_bytes(fake.gmail_drafts[0], policy=default_policy)
    assert msg["Subject"] == "Weekly shipment report"
    assert msg["To"] is None                                  # the user adds recipients in Gmail
    assert msg["From"].addresses[0].addr_spec == "seller@gmail.com"
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "Shipment Report.pdf" and attachment.get_content() == PDF
    assert msg.get_body(("html",)).get_content().strip() == "<p>Hello</p>"

    row = db_rows("SELECT status, provider_draft_id, attachment_name, attachment_bytes, report_id FROM email_draft_log")
    assert row == [("created", "r-1", "Shipment Report.pdf", len(PDF), "rep-1")]
    columns = {r[0] for r in db_rows("SELECT column_name FROM information_schema.columns "
                                     "WHERE table_name = 'email_draft_log'")}
    assert not {"html", "text", "body"} & columns


def test_draft_without_an_attachment(client, fake):
    conn = connect(client)["connection_id"]
    res = draft(client, conn, pdf=None)
    assert res.status_code == 200, res.text
    assert list(message_from_bytes(fake.gmail_drafts[0], policy=default_policy).iter_attachments()) == []


def test_outlook_draft_with_a_small_pdf_and_the_rotated_refresh_token(client, fake):
    conn = connect(client, "outlook")
    assert conn["account_email"] == "ops@contoso.com"
    before = db_rows("SELECT encrypted_refresh_token FROM email_credentials")[0][0]
    client.app.state.email.ctx.access_tokens.drop(conn["connection_id"])   # force a refresh
    res = draft(client, conn["connection_id"])
    assert res.status_code == 200, res.text
    assert res.json()["open_url"] == "https://outlook.office365.com/owa/?ItemID=AAMk1"
    assert fake.outlook_drafts[0] == {"subject": "Weekly shipment report",
                                      "body": {"contentType": "HTML", "content": "<p>Hello</p>"}}
    assert fake.outlook_attachments[0]["name"] == "Shipment Report.pdf"
    assert db_rows("SELECT encrypted_refresh_token FROM email_credentials")[0][0] != before


def test_outlook_draft_with_a_large_pdf_uses_an_upload_session(client, fake):
    conn = connect(client, "outlook")["connection_id"]
    big = b"%PDF-" + b"0" * (7 * 1024 * 1024)
    res = draft(client, conn, pdf=big)
    assert res.status_code == 200, res.text
    assert fake.outlook_attachments[0]["AttachmentItem"]["size"] == len(big)
    ranges = [r.headers["content-range"] for r in fake.upload_chunks]
    assert ranges[0].startswith("bytes 0-") and ranges[-1].endswith(f"-{len(big) - 1}/{len(big)}")
    assert b"".join(r.content for r in fake.upload_chunks) == big
    assert all(str(r.url) == UPLOAD_URL for r in fake.upload_chunks)


def test_a_failed_outlook_attachment_removes_the_half_built_draft(client, fake):
    conn = connect(client, "outlook")["connection_id"]
    fake.next_api = [None, 500]   # draft created, attachment fails
    res = draft(client, conn)
    assert res.status_code == 502 and res.json()["detail"]["code"] == "EMAIL_PROVIDER_ERROR"
    assert fake.deleted == ["AAMk1"]
    assert db_rows("SELECT status, error_code FROM email_draft_log")[0] == ("failed", "EMAIL_PROVIDER_ERROR")


def test_internal_draft_needs_the_internal_key(client):
    conn = connect(client)["connection_id"]
    res = draft(client, conn, headers=seller())
    assert res.status_code == 403 and res.json()["detail"]["code"] == "EMAIL_INTERNAL_ONLY"


def test_another_sellers_mailbox_cannot_be_used(client):
    conn = connect(client, seller_id="S1")["connection_id"]
    res = draft(client, conn, headers=internal("S2"))
    assert res.status_code == 404 and res.json()["detail"]["code"] == "EMAIL_CONNECTION_NOT_FOUND"


def test_invalid_requests(client):
    conn = connect(client)["connection_id"]
    assert draft(client, conn, subject="a" * 300).json()["detail"]["code"] == "EMAIL_INVALID_REQUEST"
    res = draft(client, conn, pdf=b"MZ\x90 not a pdf")
    assert res.status_code == 422 and "PDF" in res.json()["detail"]["message"]
    client.app.state.email.ctx.settings.MAX_ATTACHMENT_BYTES = 1024
    assert draft(client, conn).json()["detail"]["code"] == "EMAIL_ATTACHMENT_TOO_LARGE"


def test_revoked_access_marks_the_mailbox_reconnect_required(client, fake):
    conn = connect(client)["connection_id"]
    client.app.state.email.ctx.access_tokens.drop(conn)
    fake.revoked_refresh_tokens.add("g-refresh")
    res = draft(client, conn)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "EMAIL_RECONNECT_REQUIRED"
    assert db_rows("SELECT status FROM email_connections")[0][0] == "reconnect_required"
    assert db_rows("SELECT status, error_code FROM email_draft_log")[0] == ("failed", "EMAIL_RECONNECT_REQUIRED")


def test_too_little_permission_asks_for_a_reconnect(client, fake):
    conn = connect(client)["connection_id"]
    fake.next_api = [403]
    res = draft(client, conn)
    assert res.status_code == 403 and res.json()["detail"]["code"] == "EMAIL_PERMISSION_MISSING"
    assert res.json()["detail"]["action"] == "reconnect"
    assert db_rows("SELECT status FROM email_connections")[0][0] == "reconnect_required"


def test_provider_errors_never_leak_their_details(client, fake):
    conn = connect(client)["connection_id"]
    fake.next_api = [429]
    first = draft(client, conn)
    assert first.status_code == 429 and first.json()["detail"]["code"] == "EMAIL_PROVIDER_RATE_LIMITED"
    assert first.headers["Retry-After"] == "30" and "secret details" not in first.text
    fake.next_api = ["timeout"]
    assert draft(client, conn).json()["detail"]["code"] == "EMAIL_PROVIDER_ERROR"
    assert draft(client, conn).status_code == 200


def test_hourly_limit(client):
    conn = connect(client)["connection_id"]
    client.app.state.email.ctx.settings.DRAFTS_PER_HOUR = 2
    assert draft(client, conn).status_code == 200
    assert draft(client, conn).status_code == 200
    res = draft(client, conn)
    assert res.status_code == 429 and res.json()["detail"]["code"] == "EMAIL_RATE_LIMITED"


def test_disconnect(client, fake):
    conn = connect(client)["connection_id"]
    res = client.delete(f"/api/email/connections/{conn}", headers=seller())
    assert res.status_code == 200 and res.json()["status"] == "disconnected"
    assert fake.revoked == ["g-refresh"]
    assert db_rows("SELECT encrypted_refresh_token FROM email_credentials")[0][0] is None
    assert client.get("/api/email/connections", headers=seller()).json()["connections"] == []
    assert draft(client, conn).json()["detail"]["code"] == "EMAIL_CONNECTION_NOT_FOUND"


def test_the_sign_in_tab_finds_its_way_to_the_draft(client):
    start = client.post("/api/email/connect/gmail/start", headers=seller()).json()
    state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
    page = client.get("/api/email/oauth/callback", params={"state": state, "code": "good-code"})
    assert "next?state=" in page.text and f'"{state}"' in page.text
    assert client.get("/api/email/oauth/next", params={"state": state}).json() == {"url": None}

    tx = start["transaction_id"]
    url = "https://mail.google.com/mail/?authuser=seller%40gmail.com#drafts?compose=18c0abc1"
    assert client.post(f"/api/email/connect/transactions/{tx}/next", json={"url": url},
                       headers=seller()).status_code == 204
    assert client.get("/api/email/oauth/next", params={"state": state}).json() == {"url": url}
    assert client.get("/api/email/oauth/next", params={"state": "someone-elses"}).json() == {"url": None}


def test_the_sign_in_tab_can_only_be_sent_to_a_mail_app(client):
    tx = client.post("/api/email/connect/gmail/start", headers=seller()).json()["transaction_id"]
    bad = client.post(f"/api/email/connect/transactions/{tx}/next", json={"url": "https://evil.example/x"},
                      headers=seller())
    assert bad.status_code == 422
    other = client.post(f"/api/email/connect/transactions/{tx}/next",
                        json={"url": "https://mail.google.com/mail/#drafts"}, headers=seller("S2"))
    assert other.status_code == 400 and other.json()["detail"]["code"] == "EMAIL_TRANSACTION_INVALID"
