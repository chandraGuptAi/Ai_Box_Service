"""Chat with saved conversations, like ChatGPT: /api/assistant/sessions, /chat and /usage.

Every AI call is recorded in assistant_usage (tokens per user) and refused once the user's day, week or month
limit is used up. POST /chat answers as a stream of JSON lines (application/x-ndjson):

    {"type": "session", "session": {...}}           the conversation (new or existing)
    {"type": "user_message", "message": {...}}      the saved user message
    {"type": "delta", "text": "..."}                answer pieces as the AI writes them
    {"type": "done", "message": {...}, "usage": {...}, "limits": {...}}   the saved answer, split into parts
    {"type": "error", "code": "...", "message": "..."}                    the answer failed part way
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.modules.privacy.engine import Redactor

from .common import attached_files, current_user, error, seller_details
from .engine import SYSTEM_PROMPT, AssistantError, answer_text, split_answer
from .files import FORMAT_NAMES, files_block
from .limits import Period, blocking, limit_message, usage_status
from .schemas import ChatRequest, Message, RenameRequest, Session, SessionDetail, UsageResponse
from .seller_details import fill
from .store import ChatStore

chat_router = APIRouter()
logger = logging.getLogger("module.ai_assistant")

CHAT_RULES = """
This is a conversation. Earlier messages are context: a follow-up such as "make it shorter" or "now in Hindi"
refers to your last answer. Answer only the newest message."""
TITLE_CHARS = 60


def _store(request: Request) -> ChatStore:
    store = getattr(request.app.state, "ai_assistant_chats", None)
    if store is None:
        raise error(503, "chat_history_unavailable", "Chat history is not available on this server right now.")
    return store


def estimate_tokens(text: str) -> int:
    """Rough count when the AI service reports none (Hindi takes more tokens per character than English)."""
    return math.ceil(len(text) / 3) if text else 0


def _usage_out(periods: list[Period]) -> UsageResponse:
    stop = blocking(periods)
    return UsageResponse(
        periods=[{"period": p.name, "used": p.used, "limit": p.limit, "left": p.left, "resets_at": p.resets_at}
                 for p in periods],
        blocked=stop is not None, message=limit_message(stop) if stop else None)


async def refuse_over_limit(request: Request, user: str) -> list[Period] | None:
    """429 token_limit_reached when a limit is used up. No store (no database): no limits."""
    store = getattr(request.app.state, "ai_assistant_chats", None)
    if store is None:
        return None
    periods = await usage_status(store, user, request.app.state.ai_assistant.settings)
    stop = blocking(periods)
    if stop:
        raise error(429, "token_limit_reached", limit_message(stop))
    return periods


async def _record(store: ChatStore | None, user: str, session_id: str | None, message_id: int | None, endpoint: str,
                  model: str, prompt: str, answer: str, usage: dict) -> dict[str, int]:
    prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
    estimated = not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int)
    if estimated:
        prompt_tokens, completion_tokens = estimate_tokens(prompt), estimate_tokens(answer)
    if store is not None:
        try:
            await store.record_usage(user, session_id, message_id, endpoint, model, prompt_tokens, completion_tokens,
                                     estimated)
        except Exception:  # never lose the answer because the usage row failed; the log has it
            logger.exception("Token usage not saved (user=%s, tokens=%s)", user, prompt_tokens + completion_tokens)
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


async def record_ask_usage(request: Request, user: str, model: str, messages: list[dict], answer: str,
                           usage: dict) -> None:
    store = getattr(request.app.state, "ai_assistant_chats", None)
    prompt = "\n".join(m["content"] for m in messages)
    await _record(store, user, None, None, "ask", model, prompt, answer, usage)


def _session(row: dict) -> Session:
    return Session(id=str(row["id"]), title=row["title"], created_at=row["created_at"], updated_at=row["updated_at"])


def _message(row: dict) -> Message:
    parts = row.get("parts")
    if isinstance(parts, str):
        parts = json.loads(parts)
    files = row.get("files") or []
    if isinstance(files, str):
        files = json.loads(files)
    shown = parts and answer_text(parts)
    return Message(id=row["id"], role=row["role"], content=shown or row["content"], parts=parts, files=files,
                   created_at=row["created_at"])


def _title(text: str, files: list[dict]) -> str:
    text = " ".join(text.split())
    if not text:
        return f"File: {files[0]['name']}"[:TITLE_CHARS] if files else "New chat"
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


def history_messages(rows: list[dict], max_messages: int, max_chars: int) -> list[dict[str, str]]:
    """Earlier turns for the AI, newest kept first: at most max_messages and about max_chars.

    The user's own words and the AI's raw answers (with [shop name]-style placeholders, not the seller's details)
    are sent; an older file's text is sent only while it fits, else just its name.
    """
    picked: list[dict[str, str]] = []
    used = 0
    for row in reversed(rows[-max_messages:]):
        text = row["content"] or ("Summarise the attached file(s)." if row["role"] == "user" else "")
        context = row.get("file_context") or ""
        if context and used + len(text) + len(context) <= max_chars:
            text += context
        elif context:
            names = ", ".join(f["name"] for f in (row.get("files") or []) if isinstance(f, dict))
            text += f"\n\n[Earlier attached file(s) {names}: content no longer included]"
        if used + len(text) > max_chars and picked:
            break
        picked.append({"role": row["role"], "content": text[-max_chars:]})
        used += len(text)
    picked.reverse()
    # The AI expects the conversation to start with the user.
    while picked and picked[0]["role"] != "user":
        picked.pop(0)
    return picked


def _line(event: dict[str, Any]) -> bytes:
    return (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")


# ---- conversations ----

@chat_router.get("/sessions", response_model=list[Session], summary="The user's conversations, latest first")
async def list_sessions(request: Request, user: str = Depends(current_user),
                        limit: int = Query(50, ge=1, le=200)) -> list[Session]:
    return [_session(r) for r in await _store(request).list_sessions(user, limit)]


@chat_router.get("/sessions/{session_id}", response_model=SessionDetail, summary="One conversation with its messages")
async def get_session(session_id: str, request: Request, user: str = Depends(current_user)) -> SessionDetail:
    store = _store(request)
    row = await store.get_session(user, session_id)
    if not row:
        raise error(404, "session_not_found", "This chat was not found. It may have been deleted.")
    return SessionDetail(session=_session(row), messages=[_message(m) for m in await store.messages(str(row["id"]))])


@chat_router.patch("/sessions/{session_id}", response_model=Session, summary="Rename a conversation")
async def rename_session(session_id: str, body: RenameRequest, request: Request,
                         user: str = Depends(current_user)) -> Session:
    row = await _store(request).rename_session(user, session_id, body.title)
    if not row:
        raise error(404, "session_not_found", "This chat was not found. It may have been deleted.")
    return _session(row)


@chat_router.delete("/sessions/{session_id}", status_code=204, response_class=Response,
                    summary="Delete a conversation (its token usage stays recorded)")
async def delete_session(session_id: str, request: Request, user: str = Depends(current_user)) -> Response:
    if not await _store(request).delete_session(user, session_id):
        raise error(404, "session_not_found", "This chat was not found. It may have been deleted.")
    return Response(status_code=204)


@chat_router.get("/usage", response_model=UsageResponse, summary="Tokens used and left today, this week, this month")
async def usage(request: Request, user: str = Depends(current_user)) -> UsageResponse:
    assistant = getattr(request.app.state, "ai_assistant", None)
    if assistant is None:
        raise error(503, "ai_assistant_not_configured", "The AI assistant is not set up on this server yet.")
    return _usage_out(await usage_status(_store(request), user, assistant.settings))


# ---- messages ----

@chat_router.post("/chat", summary="Send a message; the answer streams back as JSON lines (see module docs)",
                  response_class=StreamingResponse)
async def chat(body: ChatRequest, request: Request, user: str = Depends(current_user),
               x_shipment_authorization: str | None = Header(default=None)) -> StreamingResponse:
    assistant = getattr(request.app.state, "ai_assistant", None)
    if assistant is None:
        raise error(503, "ai_assistant_not_configured", "The AI assistant is not set up on this server yet.")
    store = _store(request)
    settings = assistant.settings
    await refuse_over_limit(request, user)

    if body.session_id:
        session = await store.get_session(user, body.session_id)
        if not session:
            raise error(404, "session_not_found", "This chat was not found. It may have been deleted.")
        earlier = await store.messages(str(session["id"]))
    else:
        session, earlier = None, []

    files = await attached_files(request, user, body.attachment_ids, body.content)
    file_names = []
    for meta in files:
        kind = (meta.get("inspection") or {}).get("format") or "file"
        file_names.append({"name": meta.get("filename") or "attachment", "format": FORMAT_NAMES.get(kind, kind)})
    file_context = files_block(files)
    content = body.content.strip()
    instruction = content or "Summarise the attached file(s)."

    # Same privacy filter as every other AI call: personal data in the conversation and the files is masked.
    conversation = [{"role": "system", "content": SYSTEM_PROMPT + CHAT_RULES},
                    *history_messages(earlier, settings.HISTORY_MESSAGES, settings.HISTORY_CHARS),
                    {"role": "user", "content": instruction + file_context}]
    redactor = Redactor((), request.app.state.privacy_detector)
    messages = await run_in_threadpool(redactor.messages, conversation)

    # Start the AI before answering, so connection and limit errors are plain HTTP errors, not stream events.
    pieces = assistant.stream(messages)
    try:
        first = await pieces.__anext__()
    except AssistantError as exc:
        raise error(exc.status, exc.code, exc.message) from None

    if session is None:
        session = await store.create_session(user, _title(content, file_names))
    session_id = str(session["id"])
    user_row = await store.add_message(session_id, "user", content, files=file_names, file_context=file_context)
    details = asyncio.create_task(
        seller_details(getattr(request.app.state, "ai_assistant_identity", None), x_shipment_authorization))
    prompt_text = "\n".join(m["content"] for m in messages)

    async def events() -> AsyncIterator[bytes]:
        written: list[str] = []
        finished = False
        try:
            yield _line({"type": "session", "session": _session(session).model_dump(mode="json")})
            yield _line({"type": "user_message", "message": _message(user_row).model_dump(mode="json")})
            event = first
            while True:
                if event["type"] == "delta":
                    written.append(event["text"])
                    yield _line(event)
                else:
                    break
                event = await pieces.__anext__()

            raw = event["content"]
            truncated = event.get("finish_reason") == "length"
            try:
                parts = split_answer(raw)
            except AssistantError as exc:
                finished = True
                await _record(store, user, session_id, None, "chat", assistant.model, prompt_text, raw,
                              event.get("usage") or {})
                yield _line({"type": "error", "code": exc.code, "message": exc.message})
                return
            values = await details
            parts = [{**p, "text": fill(p["text"], values)} for p in parts]
            row = await store.add_message(session_id, "assistant", raw, parts=parts)
            finished = True
            used = await _record(store, user, session_id, row["id"], "chat", assistant.model, prompt_text, raw,
                                 event.get("usage") or {})
            periods = await usage_status(store, user, settings)
            yield _line({"type": "done", "message": _message(row).model_dump(mode="json"), "usage": used,
                         "truncated": truncated, "limits": _usage_out(periods).model_dump(mode="json")})
        except AssistantError as exc:
            yield _line({"type": "error", "code": exc.code, "message": exc.message})
        finally:
            if not details.done():
                details.cancel()
            if not finished:
                # Failed or the user left part way: the AI still used tokens, so record an estimate and keep
                # what was written. A task, because this generator may be closing.
                partial = "".join(written)
                asyncio.ensure_future(_save_partial(store, user, session_id, assistant.model, prompt_text, partial))

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def _save_partial(store: ChatStore, user: str, session_id: str, model: str, prompt: str, partial: str) -> None:
    message_id = None
    try:
        if partial.strip():
            parts = split_answer(partial)
            row = await store.add_message(session_id, "assistant", partial, parts=parts)
            message_id = row["id"]
    except Exception:  # an unusable fragment: only the usage is kept
        logger.info("Partial answer not saved (session=%s)", session_id)
    await _record(store, user, session_id, message_id, "chat", model, prompt, partial, {})
