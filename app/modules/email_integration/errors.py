"""Machine-readable email errors: ``{"detail": {"code", "message", "retryable", "action"?, ...}}``.

Provider error bodies, tokens and stack traces never reach a response.
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

# code -> (HTTP status, default user-facing message, action the UI can offer)
CODES: dict[str, tuple[int, str, str | None]] = {
    "EMAIL_NOT_CONFIGURED": (503, "Email is not configured on this server yet.", None),
    "EMAIL_PROVIDER_NOT_CONFIGURED": (503, "{provider} is not configured on this server yet.", None),
    "EMAIL_PROVIDER_UNSUPPORTED": (404, "Unknown email provider.", None),
    "EMAIL_SELLER_UNAUTHENTICATED": (401, "Your BigShip login could not be verified. Please log in again.", "login"),
    "EMAIL_IDENTITY_UNAVAILABLE": (503, "Your BigShip login cannot be verified right now. Please try again.",
                                   "retry_later"),
    "EMAIL_INTERNAL_ONLY": (403, "This endpoint is only available to the AI Box backend.", None),
    "EMAIL_TRANSACTION_INVALID": (400, "This sign-in attempt expired or was already used. Start again.", "connect"),
    "EMAIL_AUTHORIZATION_DENIED": (400, "Access was not granted in {provider}.", "connect"),
    "EMAIL_AUTHORIZATION_FAILED": (400, "{provider} sign-in could not be completed. Please try again.", "connect"),
    "EMAIL_PERMISSION_MISSING": (403, "{provider} did not give AI Box permission to create drafts. Click Send to "
                                      "again and, on the {provider} permission screen, tick the box that lets AI Box "
                                      "manage drafts.", "reconnect"),
    "EMAIL_CONNECTION_NOT_FOUND": (404, "Mailbox connection not found.", "connect"),
    "EMAIL_RECONNECT_REQUIRED": (409, "Your {provider} access has expired. Connect the mailbox again.", "reconnect"),
    "EMAIL_INVALID_REQUEST": (422, "The request is invalid.", None),
    "EMAIL_ATTACHMENT_TOO_LARGE": (413, "The attachment is too large.", None),
    "EMAIL_RATE_LIMITED": (429, "Too many drafts in the last hour. Please try again later.", "retry_later"),
    "EMAIL_PROVIDER_RATE_LIMITED": (429, "{provider} is limiting requests right now. Please try again shortly.",
                                    "retry_later"),
    "EMAIL_DRAFT_REJECTED": (422, "{provider} did not accept the draft.", None),
    "EMAIL_PROVIDER_ERROR": (502, "{provider} returned an error. Please try again later.", "retry_later"),
}

PROVIDER_NAMES = {"gmail": "Gmail", "outlook": "Outlook"}
RETRYABLE = {"EMAIL_IDENTITY_UNAVAILABLE", "EMAIL_PROVIDER_RATE_LIMITED", "EMAIL_PROVIDER_ERROR", "EMAIL_RATE_LIMITED"}


class EmailError(Exception):
    def __init__(self, code: str, message: str | None = None, *, provider: str | None = None,
                 retry_after: int | None = None, details: dict[str, Any] | None = None) -> None:
        status, default, action = CODES[code]
        self.code, self.status, self.provider, self.action = code, status, provider, action
        self.message = message or default.format(provider=PROVIDER_NAMES.get(provider or "", "The email provider"))
        self.retry_after = retry_after
        self.details = details or {}
        super().__init__(f"{code}: {self.message}")

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def body(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        for key, value in (("provider", self.provider), ("action", self.action),
                           ("retry_after_seconds", self.retry_after), ("details", self.details)):
            if value:
                detail[key] = value
        return {"detail": detail}

    def response(self) -> JSONResponse:
        headers = {"Retry-After": str(self.retry_after)} if self.retry_after else None
        return JSONResponse(self.body(), status_code=self.status, headers=headers)
