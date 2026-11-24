"""Provider interface and the normalized types every email provider uses.

A provider knows how to: build the OAuth authorization URL, exchange/refresh tokens, read the mailbox
address, create one draft (body + attachment) and return the link that opens it, and (best effort) revoke
access. It never touches the database.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from app.core.logger import get_logger

logger = get_logger("module.email_integration.providers")

# What went wrong, in terms the draft service can act on:
#   invalid_grant  refresh token revoked/expired -> mark the mailbox reconnect_required
#   permission     the account did not grant draft permission (e.g. connected with an older scope)
#   rate_limited   provider throttled us
#   rejected       provider refused this request (too large, invalid, ...)
#   unavailable    provider unreachable or answered with a server error
#   auth_failed    sign-in could not be completed
ErrorKind = Literal["invalid_grant", "permission", "rate_limited", "rejected", "unavailable", "auth_failed"]


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, status: int | None = None, retry_after: int | None = None,
                 reason: str = "") -> None:
        self.kind, self.status, self.retry_after, self.reason = kind, status, retry_after, reason
        super().__init__(f"{kind} (HTTP {status}) {reason}".strip())


@dataclass
class OAuthTokens:
    access_token: str
    expires_in: int
    refresh_token: str | None
    scopes: list[str]


@dataclass
class MailboxProfile:
    email: str
    name: str | None = None


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class OutgoingEmail:
    """A draft: recipients are optional, the user adds them in Gmail/Outlook."""
    from_email: str
    subject: str
    html: str
    text: str
    to: list[str] = field(default_factory=list)
    from_name: str | None = None
    attachment: Attachment | None = None


@dataclass
class DraftResult:
    draft_id: str
    #: Opens the draft in Gmail / Outlook on the web, ready to edit and send.
    open_url: str


class BaseEmailProvider(ABC):
    name: str = ""

    def __init__(self, settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http

    @abstractmethod
    def authorization_url(self, state: str, code_challenge: str) -> str: ...

    @abstractmethod
    async def exchange_code(self, code: str, code_verifier: str) -> OAuthTokens: ...

    @abstractmethod
    async def refresh(self, refresh_token: str) -> OAuthTokens: ...

    @abstractmethod
    async def profile(self, access_token: str) -> MailboxProfile: ...

    @abstractmethod
    async def create_draft(self, access_token: str, email: OutgoingEmail) -> DraftResult: ...

    async def revoke(self, token: str) -> None:
        """Best effort; providers without a revoke endpoint do nothing."""

    # ---- shared HTTP helpers ---------------------------------------------------------------------

    async def _token_request(self, url: str, data: dict[str, str]) -> OAuthTokens:
        try:
            res = await self.http.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            logger.warning("%s token request failed: %s", self.name, type(exc).__name__)
            raise ProviderError("unavailable") from None
        body = _json(res)
        if res.status_code != 200:
            error = str(body.get("error", ""))
            logger.warning("%s token request rejected: HTTP %s %s", self.name, res.status_code, error[:60])
            raise ProviderError("invalid_grant" if error == "invalid_grant" else "auth_failed", res.status_code,
                                reason=error[:60])
        if not body.get("access_token"):
            raise ProviderError("auth_failed", res.status_code, reason="no access_token")
        return OAuthTokens(access_token=str(body["access_token"]), expires_in=int(body.get("expires_in") or 3600),
                           refresh_token=body.get("refresh_token"), scopes=str(body.get("scope", "")).split())

    async def _api_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Call a mail API and turn failures into ProviderError (no provider message reaches the caller)."""
        try:
            res = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("%s %s: no answer (%s)", self.name, method, type(exc).__name__)
            raise ProviderError("unavailable") from None
        if res.status_code < 300:
            return res
        reason = _error_reason(_json(res))
        logger.warning("%s %s: HTTP %s %s", self.name, method, res.status_code, reason)
        if res.status_code == 429:
            raise ProviderError("rate_limited", 429, _retry_after(res), reason)
        if res.status_code == 401:
            raise ProviderError("invalid_grant", 401, reason=reason)
        if res.status_code == 403:
            raise ProviderError("permission", 403, reason=reason)
        if 400 <= res.status_code < 500:
            raise ProviderError("rejected", res.status_code, reason=reason)
        raise ProviderError("unavailable", res.status_code, reason=reason)


def _json(res: httpx.Response) -> dict[str, Any]:
    try:
        body = res.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_reason(body: dict[str, Any]) -> str:
    """A short, non-sensitive reason code from a Google/Microsoft error body (never the full message)."""
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("status") or "")[:60]
    return str(error or "")[:60]


def _retry_after(res: httpx.Response) -> int | None:
    try:
        return max(1, min(3600, int(res.headers.get("Retry-After", ""))))
    except ValueError:
        return None
