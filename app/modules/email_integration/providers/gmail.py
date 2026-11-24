"""Gmail: Google OAuth 2.0 (authorization code + PKCE) and the Gmail API ``drafts.create``.

Scopes: ``openid email`` (to learn the mailbox address) and ``gmail.compose`` (create drafts). Google
classes ``gmail.compose`` as *restricted*: fine for test users while the app is in Testing; a published app
needs Google's verification and security assessment.

The draft goes as raw MIME to the media upload endpoint (``uploadType=media``, messages up to 35 MB). It is
then opened in Gmail on the web, in the signed-in account (``authuser``), ready for the user to add
recipients and send.
"""
from __future__ import annotations

from urllib.parse import quote, urlencode

from ..mime import build_mime
from .base import (BaseEmailProvider, DraftResult, MailboxProfile, OAuthTokens, OutgoingEmail, ProviderError,
                   _json)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRAFTS_URL = "https://gmail.googleapis.com/upload/gmail/v1/users/me/drafts?uploadType=media"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
SCOPES = ("openid", "email", COMPOSE_SCOPE)


def draft_url(account_email: str, message_id: str) -> str:
    """Gmail web link that opens this draft in the compose window of the right account."""
    return f"https://mail.google.com/mail/?authuser={quote(account_email)}#drafts?compose={quote(message_id)}"


class GmailProvider(BaseEmailProvider):
    name = "gmail"

    def authorization_url(self, state: str, code_challenge: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.settings.GOOGLE_CLIENT_ID,
            "redirect_uri": self.settings.OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",             # returns a refresh token
            "prompt": "consent select_account",   # always a refresh token; lets the user pick the account
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })

    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        tokens = await self._token_request(TOKEN_URL, {
            "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
            "client_id": self.settings.GOOGLE_CLIENT_ID, "client_secret": self.settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": self.settings.OAUTH_REDIRECT_URI})
        if COMPOSE_SCOPE not in tokens.scopes:
            # The consent screen lets users untick individual permissions.
            raise ProviderError("permission", reason="gmail.compose not granted")
        if not tokens.refresh_token:
            raise ProviderError("auth_failed", reason="no refresh_token")
        return tokens

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        return await self._token_request(TOKEN_URL, {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.settings.GOOGLE_CLIENT_ID, "client_secret": self.settings.GOOGLE_CLIENT_SECRET})

    async def profile(self, access_token: str) -> MailboxProfile:
        res = await self._api_request("GET", USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        body = _json(res)
        email = str(body.get("email") or "").strip().lower()
        if not email or body.get("email_verified") is False:
            raise ProviderError("auth_failed", reason="no verified email")
        return MailboxProfile(email=email, name=body.get("name"))

    async def create_draft(self, access_token: str, email: OutgoingEmail) -> DraftResult:
        res = await self._api_request("POST", DRAFTS_URL, content=build_mime(email),
                                      headers={"Authorization": f"Bearer {access_token}",
                                               "Content-Type": "message/rfc822"})
        body = _json(res)
        message_id = str((body.get("message") or {}).get("id") or "")
        if not body.get("id") or not message_id:
            raise ProviderError("unavailable", res.status_code, reason="no draft id")
        return DraftResult(draft_id=str(body["id"]), open_url=draft_url(email.from_email, message_id))

    async def revoke(self, token: str) -> None:
        try:
            await self.http.post(REVOKE_URL, data={"token": token})
        except Exception:   # noqa: BLE001 - best effort; the stored token is deleted either way
            pass
