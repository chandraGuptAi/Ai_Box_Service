"""Creating one report draft (HTML body + PDF) in a seller's connected Gmail/Outlook mailbox.

The user then opens the draft in Gmail/Outlook (``open_url``), adds recipients and sends it there. AI Box
never sends email itself.

Order:
1. validate subject, body and attachment;
2. the mailbox must belong to the seller and be connected;
3. per-seller hourly limit;
4. access token (refreshed if needed), create the draft;
5. log the attempt (never the body). A mailbox connected with too little permission is marked
   ``reconnect_required`` so the next click reconnects it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from app.core.logger import get_logger

from ..context import EmailContext
from ..errors import EmailError
from ..mime import clean_header
from ..providers import Attachment, OutgoingEmail, ProviderError
from .connections import ConnectionService

logger = get_logger("module.email_integration.drafts")

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()-]+")


def safe_filename(name: str) -> str:
    """The last path part, with unsafe characters replaced, always ending in .pdf."""
    base = re.split(r"[\\/]", name or "")[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    stem = _FILENAME_UNSAFE.sub("_", base).strip(" ._") or "report"
    return stem[:100] + ".pdf"


class DraftService:
    def __init__(self, ctx: EmailContext, connections: ConnectionService) -> None:
        self.ctx = ctx
        self.connections = connections

    async def create(self, seller_id: str, request: dict[str, Any], attachment: tuple[str, bytes] | None) -> dict:
        subject = clean_header(request.get("subject", ""))
        if not subject or len(subject) > 200:
            raise EmailError("EMAIL_INVALID_REQUEST", "The subject must be 1-200 characters.")
        html, text = request.get("html") or "", request.get("text") or ""
        if not html.strip() or len(html.encode()) > self.ctx.settings.MAX_HTML_BYTES or len(text) > 100_000:
            raise EmailError("EMAIL_INVALID_REQUEST", "The email body is empty or too long.")
        file = self._attachment(attachment)

        row = await self.connections.owned(seller_id, request["connection_id"])
        if row["status"] != "connected":
            raise EmailError("EMAIL_RECONNECT_REQUIRED", provider=row["provider"])
        if await self.ctx.repo.drafts_in_last_hour(seller_id) >= self.ctx.settings.DRAFTS_PER_HOUR:
            raise EmailError("EMAIL_RATE_LIMITED", retry_after=600)

        email = OutgoingEmail(from_email=row["account_email"], from_name=row.get("display_name"), subject=subject,
                              html=html, text=text or "Please see the attached report.", attachment=file)
        log = {"seller_id": seller_id, "user_id": request.get("user_id"), "connection_id": row["id"],
               "provider": row["provider"], "from_email": row["account_email"], "report_id": request.get("report_id"),
               "subject": subject, "attachment_name": file.filename if file else None,
               "attachment_bytes": len(file.data) if file else None,
               "attachment_sha256": hashlib.sha256(file.data).hexdigest() if file else None}
        try:
            token = await self.connections.access_token(seller_id, row)
            draft = await self.ctx.provider(row["provider"]).create_draft(token, email)
        except EmailError as exc:
            await self.ctx.repo.log_draft(log | {"status": "failed", "error_code": exc.code})
            raise
        except ProviderError as exc:
            error = await self._provider_failure(seller_id, row, exc)
            await self.ctx.repo.log_draft(log | {"status": "failed", "error_code": error.code})
            raise error from None
        await self.ctx.repo.log_draft(log | {"status": "created", "provider_draft_id": draft.draft_id})
        await self.ctx.repo.touch_connection(str(row["id"]))
        logger.info("Draft created in %s (attachment=%s)", row["provider"], bool(file))
        return {"draft_id": draft.draft_id, "open_url": draft.open_url, "provider": row["provider"],
                "from_email": row["account_email"], "subject": subject,
                "attachment_name": file.filename if file else None}

    def _attachment(self, attachment: tuple[str, bytes] | None) -> Attachment | None:
        if attachment is None:
            return None
        name, data = attachment
        if not data.startswith(b"%PDF-"):
            raise EmailError("EMAIL_INVALID_REQUEST", "Only PDF reports can be attached.")
        limit = self.ctx.settings.MAX_ATTACHMENT_BYTES
        if len(data) > limit:
            raise EmailError("EMAIL_ATTACHMENT_TOO_LARGE",
                             f"The PDF is {len(data) / 1048576:.1f} MB; the limit is {limit / 1048576:.0f} MB.",
                             details={"limit_bytes": limit, "size_bytes": len(data)})
        return Attachment(filename=safe_filename(name), content_type="application/pdf", data=data)

    async def _provider_failure(self, seller_id: str, row: dict[str, Any], exc: ProviderError) -> EmailError:
        provider = row["provider"]
        if exc.kind in ("invalid_grant", "permission"):
            # Revoked access, or a mailbox connected with too little permission: the next click reconnects.
            await self.connections.mark_reconnect_required(seller_id, row, exc.kind)
            return EmailError("EMAIL_RECONNECT_REQUIRED" if exc.kind == "invalid_grant"
                              else "EMAIL_PERMISSION_MISSING", provider=provider)
        if exc.kind == "rate_limited":
            return EmailError("EMAIL_PROVIDER_RATE_LIMITED", provider=provider, retry_after=exc.retry_after)
        if exc.kind == "rejected" and exc.status == 413:
            return EmailError("EMAIL_ATTACHMENT_TOO_LARGE", provider=provider)
        if exc.kind == "rejected":
            return EmailError("EMAIL_DRAFT_REJECTED", provider=provider,
                              details={"reason": exc.reason} if exc.reason else None)
        return EmailError("EMAIL_PROVIDER_ERROR", provider=provider)
