"""AI calling audit log: every client's call logs, analyses and recordings from Voice AI (admin only).

Call logs carry customer names and phone numbers of every client, so each request needs the admin
key from AUDIT_LOG_API_KEY in `X-Audit-Key`; with no key configured the API is disabled.
"""
import secrets
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.core.config import get_settings

from .schemas import CallFilters, CallLogsResponse

CALL_ID = Path(..., pattern=r"^[A-Za-z0-9_-]{1,64}$", description="`call_id` from GET /call-logs")


def require_audit_key(x_audit_key: str | None = Header(default=None, alias="X-Audit-Key")) -> None:
    expected = get_settings().AUDIT_LOG_API_KEY
    if not expected:
        raise HTTPException(503, "The audit log API is not enabled on this server (AUDIT_LOG_API_KEY is empty)")
    if not x_audit_key or not secrets.compare_digest(x_audit_key.encode(), expected.encode()):
        raise HTTPException(401, "A valid X-Audit-Key header is required")


router = APIRouter(dependencies=[Depends(require_audit_key)])


def call_filters(
    client_id: str | None = Query(None, max_length=64, description="Only this client's calls"),
    type: str = Query("all", max_length=32, description="Call type; `all` for every type"),
    campaign_id: str | None = Query(None, max_length=64),
    status: str | None = Query(None, max_length=32, description="Call status (call logs only)"),
    outcome: str | None = Query(None, max_length=32, description="Analysis outcome, e.g. `callback` (analyses only)"),
    phone: str | None = Query(None, max_length=20),
    date_from: date | None = Query(None, description="First day, inclusive"),
    date_to: date | None = Query(None, description="Last day, inclusive"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> CallFilters:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    return CallFilters(client_id=client_id, type=type, campaign_id=campaign_id, status=status, outcome=outcome,
                       phone=phone, date_from=date_from, date_to=date_to, limit=limit, offset=offset)


@router.get("/call-logs", response_model=CallLogsResponse, summary="AI calling logs of every client, with a summary")
async def call_logs(request: Request, filters: CallFilters = Depends(call_filters)) -> dict[str, Any]:
    return await request.app.state.audit_log_service.call_logs(filters)


@router.get("/call-logs/export/csv", summary="Export all matching call logs as CSV",
            response_class=Response)
async def export_call_logs_csv(request: Request, filters: CallFilters = Depends(call_filters)) -> Response:
    content = await request.app.state.audit_log_service.export_call_logs_csv(filters)
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="call-logs.csv"',
                             "Cache-Control": "no-store"})


@router.get("/call-logs/export/xlsx", summary="Export formatted Excel call report", response_class=Response)
async def export_call_logs_excel(request: Request, filters: CallFilters = Depends(call_filters)) -> Response:
    from datetime import datetime, timezone, timedelta
    content = await request.app.state.audit_log_service.export_call_logs_excel(filters)
    stamp = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Bigship-Call-Report-{stamp}.xlsx"',
                             "Cache-Control": "no-store"})


@router.get("/call-logs/{call_id}", summary="One AI call: status, cost, analysis and recording")
async def call_log(request: Request, call_id: str = CALL_ID) -> dict[str, Any]:
    return await request.app.state.audit_log_service.call_log(call_id)


@router.get("/call-logs/{call_id}/recording", summary="Play or download one AI call's recording",
            response_class=StreamingResponse)
async def call_recording(request: Request, call_id: str = CALL_ID,
                         download: bool = Query(False, description="true: save as a file instead of playing")):
    upstream = await request.app.state.audit_log_service.client.open_recording(call_id, download)
    headers = {k: v for k, v in upstream.headers.items()
               if k.lower() in ("content-length", "content-disposition", "accept-ranges", "cache-control")}

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), media_type=upstream.headers.get("content-type", "application/octet-stream"),
                             headers=headers)


@router.get("/call-analyses", summary="AI call analyses (outcome per call) of every client")
async def call_analyses(request: Request, filters: CallFilters = Depends(call_filters)) -> dict[str, Any]:
    return await request.app.state.audit_log_service.call_analyses(filters)
