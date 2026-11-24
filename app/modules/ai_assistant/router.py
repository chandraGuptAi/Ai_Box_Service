"""POST /api/assistant/ask and the chat endpoints (reached through the AI_python_backend gateway)."""
import asyncio
import logging

from fastapi import APIRouter, Header, Request
from starlette.concurrency import run_in_threadpool

from app.modules.privacy.engine import Redactor

from .chat import chat_router, record_ask_usage, refuse_over_limit
from .common import attached_files, error, seller_details
from .engine import AssistantError, answer_text, build_messages
from .files import files_block
from .schemas import AskRequest, AskResponse
from .seller_details import fill

router = APIRouter()
logger = logging.getLogger("module.ai_assistant")


@router.post("/ask", response_model=AskResponse,
             summary="Do a small task directly: write an email or message, explain a medicine, work with files")
async def ask(body: AskRequest, request: Request, x_user_id: str | None = Header(default=None),
              x_shipment_authorization: str | None = Header(default=None)) -> AskResponse:
    user = (x_user_id or "").strip()
    if not user:
        raise error(401, "user_required", "X-User-Id header is missing. The browser did not send the logged-in user ID.")
    assistant = getattr(request.app.state, "ai_assistant", None)
    if assistant is None:
        raise error(503, "ai_assistant_not_configured", "The AI assistant is not set up on this server yet.")
    await refuse_over_limit(request, user)
    files = await attached_files(request, user, body.attachment_ids, body.instruction)
    instruction = body.instruction.strip() or "Summarise the attached file(s)."
    # The seller's details are looked up while the AI writes; the AI itself never sees them.
    details = asyncio.create_task(
        seller_details(getattr(request.app.state, "ai_assistant_identity", None), x_shipment_authorization))
    try:
        # Same privacy filter as every other AI call: personal data in the request and the files is masked.
        redactor = Redactor((), request.app.state.privacy_detector)
        messages = await run_in_threadpool(
            redactor.messages, build_messages(instruction, body.language, body.task, files_block(files)))
        parts, usage = await assistant.answer_with_usage(messages)
    except AssistantError as exc:
        details.cancel()
        raise error(exc.status, exc.code, exc.message) from None
    except BaseException:
        details.cancel()
        raise
    await record_ask_usage(request, user, assistant.model, messages, answer_text(parts), usage)
    values = await details
    parts = [{**part, "text": fill(part["text"], values)} for part in parts]
    return AskResponse(answer=answer_text(parts), parts=parts)


router.include_router(chat_router)
