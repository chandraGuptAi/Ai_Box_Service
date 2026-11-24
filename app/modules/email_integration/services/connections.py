"""Connecting, listing and disconnecting a seller's mailboxes, and handing out access tokens.

Connect flow (the new browser tab AI Box opened for the draft, authorization code + PKCE):
1. ``start``: the seller's request creates a short-lived transaction bound to that seller and returns the
   provider's authorization URL. Only sha256(state) is stored; the PKCE verifier is encrypted.
2. ``callback``: the provider redirects the popup to /api/email/oauth/callback. That request carries no
   seller login, so it only stores the encrypted code on the matching pending transaction.
3. ``complete``: the seller's own request (the AI Box page polls the transaction, then calls complete) consumes the
   transaction once and only then exchanges the code. A transaction started by seller A can never be
   completed by seller B.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.crypto import random_token, sha256_hex
from app.core.logger import get_logger

from ..config import PROVIDERS
from ..context import EmailContext
from ..errors import PROVIDER_NAMES, EmailError
from ..providers import ProviderError

logger = get_logger("module.email_integration.connections")

# The sign-in tab may only be sent to the mail apps' own pages (never an arbitrary URL).
DRAFT_HOSTS = {"mail.google.com", "outlook.office.com", "outlook.office365.com", "outlook.live.com"}


def connection_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"connection_id": str(row["id"]), "provider": row["provider"], "account_email": row["account_email"],
            "display_name": row.get("display_name"), "status": row["status"],
            "connected_at": row.get("connected_at"), "last_used_at": row.get("last_used_at")}


def provider_error(exc: ProviderError, provider: str) -> EmailError:
    """Map a provider failure during sign-in to a user-facing error."""
    if exc.kind == "permission":
        return EmailError("EMAIL_PERMISSION_MISSING", provider=provider)
    if exc.kind == "unavailable":
        return EmailError("EMAIL_PROVIDER_ERROR", provider=provider)
    return EmailError("EMAIL_AUTHORIZATION_FAILED", provider=provider)


class ConnectionService:
    def __init__(self, ctx: EmailContext) -> None:
        self.ctx = ctx

    # ---- read --------------------------------------------------------------------------------

    def providers(self) -> list[dict[str, Any]]:
        return [{"provider": p, "name": PROVIDER_NAMES[p], "configured": not self.ctx.settings.provider_problems(p)}
                for p in PROVIDERS]

    async def list(self, seller_id: str) -> list[dict[str, Any]]:
        return [connection_view(r) for r in await self.ctx.repo.list_connections(seller_id)]

    async def owned(self, seller_id: str, connection_id: str) -> dict[str, Any]:
        row = await self.ctx.repo.get_connection(seller_id, connection_id)
        if row is None or row["status"] == "disconnected":
            raise EmailError("EMAIL_CONNECTION_NOT_FOUND")
        return row

    # ---- connect -----------------------------------------------------------------------------

    async def start(self, seller_id: str, provider_name: str) -> dict[str, Any]:
        provider = self.ctx.ready_provider(provider_name)
        state = random_token(32)
        verifier = random_token(48)   # 64 chars; PKCE allows 43-128
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        transaction_id = str(uuid.uuid4())
        blob, version = self.ctx.cipher.encrypt(verifier, transaction_id)
        tx = await self.ctx.repo.create_transaction(seller_id, provider_name, sha256_hex(state),
                                                    self.ctx.settings.OAUTH_TRANSACTION_TTL_SECONDS, blob, version,
                                                    transaction_id)
        await self.ctx.repo.purge_transactions()
        return {"transaction_id": transaction_id, "provider": provider_name, "expires_at": tx["expires_at"],
                "authorization_url": provider.authorization_url(state, challenge)}

    async def callback(self, state: str | None, code: str | None, error: str | None) -> str:
        """Browser redirect from Google/Microsoft. Returns the small HTML page shown in the popup."""
        if not state:
            return callback_page(False, "This sign-in link is invalid. Close this window and try again.")
        state_hash = sha256_hex(state)
        denied = bool(error) or not code
        blob, version = (None, None) if denied else self.ctx.cipher.encrypt(code, state_hash)
        tx = await self.ctx.repo.authorize_transaction(state_hash, blob, version, denied)
        if tx is None:
            return callback_page(False, "This sign-in attempt expired. Close this window and try again.")
        if denied:
            return callback_page(False, "Access was not granted. You can close this window.")
        return callback_page(True, f"Signed in to {PROVIDER_NAMES[tx['provider']]}. Your draft with the report is "
                                   "being prepared and opens here in a moment.", state)

    async def set_next_url(self, seller_id: str, transaction_id: str, url: str) -> None:
        """The AI Box page tells the sign-in tab (via the transaction) to open the finished draft."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in DRAFT_HOSTS:
            raise EmailError("EMAIL_INVALID_REQUEST", "Only Gmail / Outlook draft links are allowed.")
        if not await self.ctx.repo.set_next_url(seller_id, transaction_id, url):
            raise EmailError("EMAIL_TRANSACTION_INVALID")

    async def next_url(self, state: str) -> str | None:
        """Asked by the sign-in tab itself (it only knows its state, which is secret to that tab)."""
        return await self.ctx.repo.next_url(sha256_hex(state)) if state else None

    async def transaction_status(self, seller_id: str, transaction_id: str) -> dict[str, Any]:
        tx = await self.ctx.repo.transaction_status(seller_id, transaction_id)
        if tx is None:
            raise EmailError("EMAIL_TRANSACTION_INVALID")
        return {"transaction_id": str(tx["id"]), "provider": tx["provider"], "status": tx["status"],
                "expires_at": tx["expires_at"]}

    async def complete(self, seller_id: str, provider_name: str, transaction_id: str) -> dict[str, Any]:
        provider = self.ctx.ready_provider(provider_name)
        status = await self.ctx.repo.transaction_status(seller_id, transaction_id)
        if status is not None and status["status"] == "denied":
            raise EmailError("EMAIL_AUTHORIZATION_DENIED", provider=provider_name)
        tx = await self.ctx.repo.consume_transaction(seller_id, transaction_id, provider_name)
        if tx is None:
            raise EmailError("EMAIL_TRANSACTION_INVALID")
        verifier = self.ctx.cipher.decrypt(tx["encrypted_verifier"], tx["verifier_key_version"], transaction_id)
        code = self.ctx.cipher.decrypt(tx["encrypted_code"], tx["code_key_version"], tx["state_hash"])
        try:
            tokens = await provider.exchange_code(code, verifier)
            profile = await provider.profile(tokens.access_token)
        except ProviderError as exc:
            await self.ctx.repo.audit(seller_id, "connect_failed", details={"provider": provider_name,
                                                                              "reason": exc.kind})
            raise provider_error(exc, provider_name) from None
        refresh_token = tokens.refresh_token or ""
        row = await self.ctx.repo.save_connection(
            seller_id, provider_name, profile.email, profile.name, tokens.scopes,
            lambda connection_id: self.ctx.cipher.encrypt(refresh_token, connection_id))
        self.ctx.access_tokens.put(str(row["id"]), tokens.access_token, tokens.expires_in)
        await self.ctx.repo.audit(seller_id, "connected", str(row["id"]), {"provider": provider_name})
        logger.info("Seller connected a %s mailbox", provider_name)
        return connection_view(row)

    # ---- disconnect --------------------------------------------------------------------------

    async def disconnect(self, seller_id: str, connection_id: str) -> dict[str, Any]:
        row = await self.owned(seller_id, connection_id)
        refresh_token = await self._refresh_token(connection_id)
        if refresh_token:
            await self.ctx.provider(row["provider"]).revoke(refresh_token)
        await self.ctx.repo.set_connection_status(connection_id, "disconnected")
        self.ctx.access_tokens.drop(connection_id)
        await self.ctx.repo.audit(seller_id, "disconnected", connection_id, {"provider": row["provider"]})
        return {"connection_id": connection_id, "status": "disconnected"}

    # ---- tokens ------------------------------------------------------------------------------

    async def access_token(self, seller_id: str, row: dict[str, Any]) -> str:
        """A valid access token for this mailbox, refreshing (and storing a rotated refresh token) if needed."""
        connection_id = str(row["id"])
        cached = self.ctx.access_tokens.get(connection_id)
        if cached:
            return cached
        refresh_token = await self._refresh_token(connection_id)
        if not refresh_token:
            raise EmailError("EMAIL_RECONNECT_REQUIRED", provider=row["provider"])
        try:
            tokens = await self.ctx.provider(row["provider"]).refresh(refresh_token)
        except ProviderError as exc:
            if exc.kind == "invalid_grant":
                await self.mark_reconnect_required(seller_id, row, "invalid_grant")
                raise EmailError("EMAIL_RECONNECT_REQUIRED", provider=row["provider"]) from None
            raise EmailError("EMAIL_PROVIDER_ERROR", provider=row["provider"]) from None
        if tokens.refresh_token and tokens.refresh_token != refresh_token:
            blob, version = self.ctx.cipher.encrypt(tokens.refresh_token, connection_id)
            await self.ctx.repo.update_credential(connection_id, blob, version)
        self.ctx.access_tokens.put(connection_id, tokens.access_token, tokens.expires_in)
        return tokens.access_token

    async def mark_reconnect_required(self, seller_id: str, row: dict[str, Any], reason: str) -> None:
        connection_id = str(row["id"])
        self.ctx.access_tokens.drop(connection_id)
        await self.ctx.repo.set_connection_status(connection_id, "reconnect_required", reason)
        await self.ctx.repo.audit(seller_id, "reconnect_required", connection_id, {"reason": reason})

    async def _refresh_token(self, connection_id: str) -> str | None:
        cred = await self.ctx.repo.get_credential(connection_id)
        if cred is None:
            return None
        try:
            return self.ctx.cipher.decrypt(cred["encrypted_refresh_token"], cred["key_version"], connection_id)
        except (KeyError, ValueError):
            logger.error("A stored mailbox credential could not be decrypted (key missing or data changed)")
            return None


def callback_page(ok: bool, message: str, state: str | None = None) -> str:
    """The sign-in tab's page after Google/Microsoft.

    With ``state`` it asks /api/email/oauth/next every 1.5 s (for up to 3 minutes) whether the AI Box page has
    finished the draft, then opens it in this tab. The browser may have cut the AI Box page's link to this tab
    during sign-in, so the tab finds its own way to the draft.
    """
    color = "#15803d" if ok else "#b91c1c"
    script = ""
    if ok and state:
        state_js = json.dumps(state).replace("<", "\\u003c")   # never closes the <script>
        script = f"""<script>
(function () {{
  var state = {state_js}, tries = 0;
  function ask() {{
    fetch("next?state=" + encodeURIComponent(state), {{ cache: "no-store" }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d && d.url) {{ location.replace(d.url); return; }}
        if (++tries < 120) {{ setTimeout(ask, 1500); return; }}
        document.getElementById("msg").textContent = "Your draft is ready in AI Box. You can close this tab.";
      }})
      .catch(function () {{ if (++tries < 120) setTimeout(ask, 1500); }});
  }}
  setTimeout(ask, 1000);
}})();
</script>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Box</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font:15px system-ui,sans-serif;
background:#f6f7fb;color:#172244}}p{{max-width:360px;text-align:center;padding:24px;border-radius:14px;
background:#fff;box-shadow:0 8px 30px #17224418;border-top:4px solid {color}}}</style></head>
<body><p id="msg">{html.escape(message)}</p>{script}</body></html>"""
