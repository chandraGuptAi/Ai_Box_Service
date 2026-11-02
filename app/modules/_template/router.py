from fastapi import APIRouter, Request

from .schemas import EchoRequest, EchoResponse

router = APIRouter()


@router.post("/echo", response_model=EchoResponse)
async def echo(body: EchoRequest, request: Request) -> EchoResponse:
    service = request.app.state.__MODULE_NAME___service
    return EchoResponse(message=service.echo(body.message))
