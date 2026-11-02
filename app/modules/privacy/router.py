from fastapi import APIRouter, Request

from .engine import Redactor
from .schemas import FilterRequest, FilterResponse

router = APIRouter()


@router.post("/filter", response_model=FilterResponse,
             summary="Mask personal data in AI-bound messages and list what was removed")
def filter_messages(body: FilterRequest, request: Request) -> FilterResponse:
    # Sync handler: spaCy is CPU-bound, so FastAPI runs this in its thread pool. Content is never logged.
    redactor = Redactor(body.identities, request.app.state.privacy_detector, frozenset(body.field_names))
    messages = redactor.messages([m.model_dump() for m in body.messages])
    return FilterResponse(messages=messages,
                          removed=[{"category": c, "ref": r} for r, c in redactor.removed.items()])
