from fastapi import APIRouter, Request

from .schemas import FeedbackRequest, LookupRequest, LookupResponse, RecordRequest

router = APIRouter()


# Sync handlers: embedding is CPU work, so FastAPI runs them in its thread pool. Questions are never logged.
@router.post("/lookup", response_model=LookupResponse, summary="A reusable plan and similar successful questions")
def lookup(body: LookupRequest, request: Request):
    return request.app.state.report_memory.lookup(body.prompt, body.scope_key, body.k)


@router.post("/record", summary="Remember a question, its plan and the outcome")
def record(body: RecordRequest, request: Request):
    example_id = request.app.state.report_memory.record(body.prompt, body.scope_key, body.plan, body.report_id,
                                                        body.outcome, body.cacheable)
    return {"id": example_id}


@router.post("/feedback", summary="Apply a user signal (saved, exported, shared, rerun, up, down) to a report")
def feedback(body: FeedbackRequest, request: Request):
    return {"applied": request.app.state.report_memory.feedback(body.report_id, body.signal)}


@router.get("/stats", summary="How many questions are remembered and reusable")
def stats(request: Request):
    return request.app.state.report_memory.stats()
