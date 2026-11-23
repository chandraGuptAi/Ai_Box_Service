"""Machine-readable Meta errors.

Responses follow this service's convention ``{"detail": {"code", "message", ...}}``. Raw Meta error
payloads, tokens and stack traces never reach the response; Meta's trace ID is logged instead.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

# code -> (HTTP status, default seller-facing message, default action for the UI)
CODES: dict[str, tuple[int, str, str | None]] = {
    "META_NOT_CONNECTED": (404, "Connect your {platform} account to use this data.", "connect"),
    "META_AUTHORIZATION_FAILED": (400, "Meta authorization could not be completed. Please try again.", "connect"),
    "META_PERMISSION_DENIED": (403, "The Meta account did not grant the permissions this feature needs.", "reconnect"),
    "META_TOKEN_EXPIRED": (401, "Your {platform} authorization expired. Reconnect the account.", "reconnect"),
    "META_RECONNECT_REQUIRED": (409, "Reconnect your {platform} account to continue.", "reconnect"),
    "META_RATE_LIMITED": (429, "Meta is limiting requests right now. Please try again later.", "retry_later"),
    "META_SYNC_FAILED": (502, "The {platform} data sync failed.", "retry_later"),
    "META_SYNC_IN_PROGRESS": (409, "A sync for this account is already queued or running.", None),
    "META_METRIC_UNAVAILABLE": (422, "Meta does not provide this metric for the account.", None),
    "META_ACCOUNT_NOT_ELIGIBLE": (422, "This account is not eligible (a professional/business account is required).",
                                  None),
    "META_ACCOUNT_CONNECTED_ELSEWHERE": (409, "This Meta account is already connected to another BigShip seller.",
                                         "contact_support"),
    "META_PLATFORM_NOT_CONFIGURED": (503, "{platform} connections are not configured on this server yet.", None),
    "META_PLATFORM_UNSUPPORTED": (404, "Unknown platform.", None),
    "META_TRANSACTION_INVALID": (400, "This connection attempt expired or was already used. Start again.", "connect"),
    "META_CONNECTION_NOT_FOUND": (404, "Connection not found.", None),
    "META_JOB_NOT_FOUND": (404, "Sync job not found.", None),
    "META_PROVIDER_ERROR": (502, "Meta returned an error. Please try again later.", "retry_later"),
    "META_SELLER_RATE_LIMITED": (429, "Too many requests. Please wait a minute.", "retry_later"),
    "META_INVALID_REQUEST": (422, "The request is invalid.", None),
    "META_SELLER_UNAUTHENTICATED": (401, "Your BigShip login could not be verified. Please log in again.", "login"),
    "META_IDENTITY_UNAVAILABLE": (503, "Your BigShip login cannot be verified right now. Please try again.",
                                  "retry_later"),
}

_NAMES = {"whatsapp": "WhatsApp Business", "instagram": "Instagram professional", "facebook": "Facebook Page"}


class MetaError(Exception):
    def __init__(self, code: str, message: str | None = None, *, platform: str | None = None,
                 action: str | None = None, retry_after: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        status, default, default_action = CODES[code]
        self.code, self.status, self.platform = code, status, platform
        self.message = message or default.format(platform=_NAMES.get(platform or "", "Meta"))
        self.action = action if action is not None else default_action
        self.retry_after = retry_after
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")

    @property
    def retryable(self) -> bool:
        return self.code in {"META_RATE_LIMITED", "META_SYNC_FAILED", "META_PROVIDER_ERROR", "META_IDENTITY_UNAVAILABLE"}

    def body(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.platform:
            detail["platform"] = self.platform
        if self.action:
            detail["action"] = self.action
        if self.retry_after:
            detail["retry_after_seconds"] = self.retry_after
        if self.details:
            detail["details"] = self.details
        return {"detail": detail}

    def response(self) -> JSONResponse:
        headers = {"Retry-After": str(self.retry_after)} if self.retry_after else None
        return JSONResponse(self.body(), status_code=self.status, headers=headers)


def error_responses(*codes: str) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses=`` entries for the listed codes."""
    out: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        status = CODES[code][0]
        entry = out.setdefault(status, {"description": "", "content": {"application/json": {"example": {}}}})
        entry["description"] = ", ".join(filter(None, [entry["description"], code]))
        entry["content"]["application/json"]["example"] = MetaError(code, platform="whatsapp").body()
    return out
