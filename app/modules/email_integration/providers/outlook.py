"""Outlook / Microsoft 365: Microsoft identity platform OAuth 2.0 (code + PKCE) and Graph drafts.

Scopes: ``openid email offline_access User.Read Mail.ReadWrite``. ``Mail.ReadWrite`` is what Graph requires
to create a draft message. Microsoft rotates refresh tokens: every refresh may return a new one, which the
caller must store.

Draft steps:
1. ``POST /me/messages`` creates the draft (subject + HTML body) and returns its ``webLink``;
2. the PDF is added: inline up to 3 MB, larger files through an upload session (chunks of 320 KiB multiples);
3. ``webLink`` opens the draft in Outlook on the web for the user to add recipients and send.
"""
from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlencode

from .base import (BaseEmailProvider, DraftResult, MailboxProfile, OAuthTokens, OutgoingEmail, ProviderError,
                   _json)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ("openid", "email", "offline_access", "User.Read", "Mail.ReadWrite")
INLINE_ATTACHMENT_LIMIT = 3 * 1024 * 1024
UPLOAD_CHUNK = 10 * 320 * 1024   # Graph needs chunk sizes in multiples of 320 KiB
DRAFTS_FALLBACK_URL = "https://outlook.office.com/mail/drafts"


def _has_scope(scopes: list[str], wanted: str) -> bool:
    # Microsoft may return short ("Mail.ReadWrite") or full ("https://graph.microsoft.com/Mail.ReadWrite") names.
    return any(s.lower().rsplit("/", 1)[-1] == wanted.lower() for s in scopes)


class OutlookProvider(BaseEmailProvider):
    name = "outlook"

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.settings.MICROSOFT_TENANT or 'common'}/oauth2/v2.0"

    def authorization_url(self, state: str, code_challenge: str) -> str:
        return f"{self._authority}/authorize?" + urlencode({
            "client_id": self.settings.MICROSOFT_CLIENT_ID,
            "redirect_uri": self.settings.OAUTH_REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "scope": " ".join(SCOPES),
            "prompt": "select_account",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })

    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens:
        tokens = await self._token_request(f"{self._authority}/token", {
            "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
            "client_id": self.settings.MICROSOFT_CLIENT_ID, "client_secret": self.settings.MICROSOFT_CLIENT_SECRET,
            "redirect_uri": self.settings.OAUTH_REDIRECT_URI, "scope": " ".join(SCOPES)})
        if not _has_scope(tokens.scopes, "Mail.ReadWrite"):
            raise ProviderError("permission", reason="Mail.ReadWrite not granted")
        if not tokens.refresh_token:
            raise ProviderError("auth_failed", reason="no refresh_token")
        return tokens

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        return await self._token_request(f"{self._authority}/token", {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.settings.MICROSOFT_CLIENT_ID, "client_secret": self.settings.MICROSOFT_CLIENT_SECRET,
            "scope": " ".join(SCOPES)})

    async def profile(self, access_token: str) -> MailboxProfile:
        res = await self._api_request("GET", f"{GRAPH}/me?$select=displayName,mail,userPrincipalName",
                                      headers={"Authorization": f"Bearer {access_token}"})
        body = _json(res)
        # Personal Microsoft accounts often have no `mail`; their sign-in name is the address.
        email = str(body.get("mail") or body.get("userPrincipalName") or "").strip().lower()
        if "@" not in email:
            raise ProviderError("auth_failed", reason="no mailbox address")
        return MailboxProfile(email=email, name=body.get("displayName"))

    async def create_draft(self, access_token: str, email: OutgoingEmail) -> DraftResult:
        auth = {"Authorization": f"Bearer {access_token}"}
        message: dict[str, Any] = {"subject": email.subject, "body": {"contentType": "HTML", "content": email.html}}
        if email.to:
            message["toRecipients"] = [{"emailAddress": {"address": a}} for a in email.to]
        created = _json(await self._api_request("POST", f"{GRAPH}/me/messages", json=message, headers=auth))
        draft_id = str(created.get("id") or "")
        if not draft_id:
            raise ProviderError("unavailable", reason="no draft id")
        if email.attachment:
            try:
                await self._attach(auth, draft_id, email)
            except ProviderError:
                # Never leave a half-built draft behind.
                try:
                    await self.http.delete(f"{GRAPH}/me/messages/{quote(draft_id)}", headers=auth)
                except Exception:   # noqa: BLE001
                    pass
                raise
        return DraftResult(draft_id=draft_id, open_url=str(created.get("webLink") or DRAFTS_FALLBACK_URL))

    async def _attach(self, auth: dict[str, str], draft_id: str, email: OutgoingEmail) -> None:
        file = email.attachment
        assert file is not None
        base = f"{GRAPH}/me/messages/{quote(draft_id)}/attachments"
        if len(file.data) <= INLINE_ATTACHMENT_LIMIT:
            await self._api_request("POST", base, headers=auth, json={
                "@odata.type": "#microsoft.graph.fileAttachment", "name": file.filename,
                "contentType": file.content_type,
                "contentBytes": base64.b64encode(file.data).decode()})
            return
        session = _json(await self._api_request("POST", f"{base}/createUploadSession", headers=auth, json={
            "AttachmentItem": {"attachmentType": "file", "name": file.filename, "size": len(file.data),
                               "contentType": file.content_type}}))
        upload_url = session.get("uploadUrl")
        if not upload_url:
            raise ProviderError("unavailable", reason="no uploadUrl")
        total = len(file.data)
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = file.data[start:start + UPLOAD_CHUNK]
            # The upload URL is pre-authorized: sending the bearer token to it is not allowed.
            await self._api_request("PUT", upload_url, content=chunk, headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"})
