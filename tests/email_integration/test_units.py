"""Unit tests that need no database: MIME building, file names, settings, BigShip identity."""
from __future__ import annotations

import asyncio
from email import message_from_bytes
from email.policy import default as default_policy

import httpx
import pytest

from app.core.seller_identity import SellerIdentity, SellerIdentityError
from app.modules.email_integration.config import EmailSettings
from app.modules.email_integration.mime import build_mime
from app.modules.email_integration.providers import Attachment, OutgoingEmail
from app.modules.email_integration.providers.gmail import draft_url
from app.modules.email_integration.services.drafts import safe_filename


def test_mime_headers_cannot_be_injected():
    raw = build_mime(OutgoingEmail(from_email="me@x.co", from_name="Me\r\nBcc: evil@x.co",
                                   subject="Report\r\nBcc: evil@x.co", html="<b>hi</b>", text="hi",
                                   attachment=Attachment("r.pdf", "application/pdf", b"%PDF-1")))
    msg = message_from_bytes(raw, policy=default_policy)
    assert msg["Bcc"] is None and msg["To"] is None
    assert msg["Subject"] == "Report Bcc: evil@x.co"
    assert [p.get_content_type() for p in msg.walk()] == [
        "multipart/mixed", "multipart/alternative", "text/plain", "text/html", "application/pdf"]


def test_gmail_draft_url_selects_the_account():
    assert draft_url("a+b@gmail.com", "18c") == "https://mail.google.com/mail/?authuser=a%2Bb%40gmail.com#drafts?compose=18c"


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd.pdf"
    assert safe_filename(r"C:\temp\x<y>.PDF") == "x_y.pdf"
    assert safe_filename("Shipment Report (Nov).pdf") == "Shipment Report (Nov).pdf"
    assert safe_filename("") == "report.pdf"


def test_provider_problems(monkeypatch):
    monkeypatch.setenv("EMAIL_SELLER_IDENTITY", "header")
    settings = EmailSettings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="", OAUTH_REDIRECT_URI="")
    assert "EMAIL_GOOGLE_CLIENT_ID" in settings.provider_problems("gmail")
    assert "EMAIL_OAUTH_REDIRECT_URI" in settings.provider_problems("gmail")
    ready = EmailSettings(MICROSOFT_CLIENT_ID="a", MICROSOFT_CLIENT_SECRET="b",
                          OAUTH_REDIRECT_URI="http://127.0.0.1:8200/api/email/oauth/callback")
    assert ready.provider_problems("outlook") == []


def _identity(handler) -> SellerIdentity:
    return SellerIdentity("bigship", "https://bigship.test", 300, transport=httpx.MockTransport(handler))


def test_bigship_identity_uses_the_profile_and_caches_it():
    calls = []

    def handler(request):
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json={"data": {"user_profile_data": {"cust_id": 4242}}})
    identity = _identity(handler)
    token = "Bearer 123|abcdefghijkl"
    assert asyncio.run(identity.resolve(token, "999")) == "4242"     # X-User-Id is ignored
    assert asyncio.run(identity.resolve(token, None)) == "4242"
    assert len(calls) == 1


@pytest.mark.parametrize("status, kind", [(401, "unauthenticated"), (500, "unavailable")])
def test_bigship_identity_errors(status, kind):
    identity = _identity(lambda request: httpx.Response(status))
    with pytest.raises(SellerIdentityError) as exc:
        asyncio.run(identity.resolve("Bearer 123|abcdefghijkl", None))
    assert exc.value.kind == kind
    with pytest.raises(SellerIdentityError) as missing:
        asyncio.run(identity.resolve(None, "S1"))
    assert missing.value.kind == "unauthenticated"
