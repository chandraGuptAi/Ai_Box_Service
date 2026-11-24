"""Helpers shared by /ask and the chat endpoints."""
import logging

from fastapi import Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.seller_identity import SellerIdentity, SellerIdentityError

from .seller_details import seller_values

logger = logging.getLogger("module.ai_assistant")


def current_user(x_user_id: str | None = Header(default=None)) -> str:
    user = (x_user_id or "").strip()
    if not user:
        raise error(401, "user_required", "X-User-Id header is missing. The browser did not send the logged-in user ID.")
    return user


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


async def seller_details(identity: SellerIdentity | None, authorization: str | None) -> dict[str, str]:
    """The logged-in seller's details for [shop name] etc.; empty (placeholders stay) when unavailable."""
    if identity is None or not authorization:
        return {}
    try:
        return seller_values(await identity.profile(authorization))
    except SellerIdentityError as exc:
        logger.info("Seller details not filled: %s", exc.kind)
    except Exception:  # never fail an answer because of the profile
        logger.warning("Seller details not filled: unexpected error", exc_info=True)
    return {}


async def attached_files(request: Request, user: str, attachment_ids: list[str], prompt: str) -> list[dict]:
    """The user's own uploaded files (attachments module), with scanned pages read first when possible."""
    if not attachment_ids:
        return []
    files = getattr(request.app.state, "attachments_service", None)
    if files is None:
        raise error(503, "attachments_unavailable", "Attached files cannot be read on this server right now.")

    async def load() -> list[dict]:
        try:
            return [(await run_in_threadpool(files.store.get, user, identifier))[0]
                    for identifier in attachment_ids]
        except HTTPException as exc:
            if exc.status_code == 404:
                raise error(410, "attachment_expired",
                             "An attached file has expired or was removed. Please attach it again.") from None
            raise

    metas = await load()
    scanned = [meta["id"] for meta in metas
               if meta["inspection"].get("requires_vision") and not meta["inspection"].get("vision_prepared")]
    if scanned and files.vision.configured:
        from app.modules.attachments.schemas import ContextRequest
        try:
            await files.prepare(user, ContextRequest(prompt=prompt.strip() or "Read the attached files",
                                                     attachment_ids=scanned))
            metas = await load()
        except HTTPException as exc:   # too many pages, vision down...: answer from the text that was found
            logger.info("Scanned pages not read (HTTP %s)", exc.status_code)
    return metas


