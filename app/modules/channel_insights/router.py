from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.security import bigship_authorization

from .schemas import AskRequest, InsightResponse
from .service import SUGGESTIONS

router = APIRouter()


@router.get("/suggestions", response_model=list[str], summary="The suggested questions shown under the box")
async def suggestions() -> list[str]:
    return SUGGESTIONS


@router.post("/ask", response_model=InsightResponse,
             summary="Answer a question about the seller's open store orders (latest data from Bigship)")
async def ask(body: AskRequest, request: Request,
              authorization: str = Depends(bigship_authorization)) -> InsightResponse:
    service = getattr(request.app.state, "channel_insights_service", None)
    if service is None:
        raise HTTPException(503, "Channel insights are not available on this server")
    assistant = getattr(request.app.state, "ai_assistant", None)
    return await service.ask(authorization, body.question, body.channel_id, assistant)
