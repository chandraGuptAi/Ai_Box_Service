from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.security import bigship_authorization

from .clean import clean_rows
from .errors import FetchError
from .fetch import Query
from .schemas import FetchRequest, FetchResponse

router = APIRouter()


@router.post("/fetch", response_model=FetchResponse,
             summary="Every shipment matching the query, without personal data")
async def fetch_shipments(body: FetchRequest, request: Request,
                          authorization: str = Depends(bigship_authorization)):
    state = request.app.state
    query = Query(status_type=body.status_type, from_date=body.from_date, to_date=body.to_date,
                  date_filter_type=body.date_filter_type, segment_type=body.segment_type)
    key = state.shipments_cache.key(authorization, {**asdict(query), "max_records": body.max_records})
    cached = None if body.fresh else state.shipments_cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}
    try:
        result = await state.shipments_fetcher.fetch(query, authorization, body.max_records)
    except FetchError as exc:
        # Same code as AI_python_backend's error classes; the client maps it back.
        return JSONResponse({"detail": {"code": exc.code, "message": exc.message, "details": exc.details,
                                        "retryable": exc.retryable}}, status_code=exc.status)
    rows, removed = clean_rows(result.rows)
    response = {"rows": rows, "total": result.total, "account_summary": result.account_summary,
                "fetched_at": result.fetched_at, "requests": result.requests, "page_size": result.page_size,
                "pages": result.pages, "attempts": result.attempts, "page_size_clamped": result.page_size_clamped,
                "removed_fields": removed}
    state.shipments_cache.put(key, response)
    return {**response, "cached": False}
