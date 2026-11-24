"""HTTP API under /api/email.

Seller endpoints learn the seller from BigShip (X-Shipment-Authorization), never from a body or X-User-Id
(except EMAIL_SELLER_IDENTITY=header in local development). The OAuth callback is authenticated by its
one-time state. POST /internal/draft also needs X-Email-Internal-Key, which only AI_python_backend has: it
builds the email body and PDF from the report, so browsers cannot put arbitrary content in a mailbox.
"""
from __future__ import annotations

import json
import secrets
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, File, Form, Header, Path, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError

from .errors import PROVIDER_NAMES, EmailError
from .schemas import (CompleteRequest, Connection, ConnectionsResponse, DisconnectResponse, DraftMetadata,
                      DraftResponse, NextUrlRequest, NextUrlResponse, Provider, ProvidersResponse, StartResponse,
                      TransactionStatus)


class EmailRoute(APIRoute):
    """Turns EmailError into the structured error body on every route of this module."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except EmailError as exc:
                return exc.response()
        return route


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(route_class=EmailRoute, dependencies=[Depends(_no_store)])


def runtime(request: Request):
    email = getattr(request.app.state, "email", None)
    if email is None:
        raise EmailError("EMAIL_NOT_CONFIGURED")
    return email


AuthHeader = Annotated[str | None, Header(alias="X-Shipment-Authorization", max_length=2100,
                                          description="The seller's BigShip login (`Bearer <token>`)")]
UserHeader = Annotated[str | None, Header(alias="X-User-Id", max_length=200,
                                          description="Ignored unless EMAIL_SELLER_IDENTITY=header")]


async def seller(authorization: AuthHeader = None, x_user_id: UserHeader = None, email=Depends(runtime)) -> str:
    return await email.ctx.seller(authorization, x_user_id)


def internal_caller(x_email_internal_key: Annotated[str | None, Header(max_length=200)] = None,
                    email=Depends(runtime)) -> None:
    expected = email.ctx.settings.INTERNAL_KEY
    if not expected or not x_email_internal_key or not secrets.compare_digest(
            x_email_internal_key.encode(), expected.encode()):
        raise EmailError("EMAIL_INTERNAL_ONLY")


Runtime = Annotated[Any, Depends(runtime)]
Seller = Annotated[str, Depends(seller)]
Uuid = Annotated[str, Path(pattern=r"^[0-9a-fA-F-]{36}$")]


# ---- providers & connections ------------------------------------------------------------------------

@router.get("/providers", response_model=ProvidersResponse, summary="Gmail/Outlook readiness and my mailboxes")
async def providers(request: Request, authorization: AuthHeader = None, x_user_id: UserHeader = None):
    email = getattr(request.app.state, "email", None)
    if email is None:
        return {"enabled": False, "providers": [
            {"provider": p, "name": PROVIDER_NAMES[p], "configured": False} for p in PROVIDER_NAMES]}
    connections: list[dict] = []
    if authorization or x_user_id:
        try:   # best effort: readiness never fails because the login could not be verified
            connections = await email.connections.list(await email.ctx.seller(authorization, x_user_id))
        except EmailError:
            connections = []
    return {"enabled": True, "providers": email.connections.providers(), "connections": connections}


@router.get("/connections", response_model=ConnectionsResponse, summary="My connected mailboxes")
async def list_connections(email: Runtime, seller_id: Seller):
    return {"connections": await email.connections.list(seller_id)}


@router.delete("/connections/{connection_id}", response_model=DisconnectResponse, summary="Disconnect a mailbox")
async def disconnect(connection_id: Uuid, email: Runtime, seller_id: Seller):
    return await email.connections.disconnect(seller_id, connection_id)


# ---- connect flow ---------------------------------------------------------------------------------

@router.post("/connect/{provider}/start", response_model=StartResponse, summary="Begin connecting a mailbox")
async def start(provider: Provider, email: Runtime, seller_id: Seller):
    return await email.connections.start(seller_id, provider)


@router.get("/connect/transactions/{transaction_id}", response_model=TransactionStatus,
            summary="Status of a connection attempt (polled while the popup is open)")
async def transaction(transaction_id: Uuid, email: Runtime, seller_id: Seller):
    return await email.connections.transaction_status(seller_id, transaction_id)


@router.post("/connect/transactions/{transaction_id}/next", status_code=204,
             summary="Send the sign-in tab to the finished draft (Gmail / Outlook links only)")
async def set_next(transaction_id: Uuid, body: NextUrlRequest, email: Runtime, seller_id: Seller):
    await email.connections.set_next_url(seller_id, transaction_id, body.url)
    return Response(status_code=204)


@router.get("/oauth/next", response_model=NextUrlResponse,
            summary="Asked by the sign-in tab: where to go once the draft is ready (by its secret state)")
async def oauth_next(email: Runtime, state: str = Query(..., max_length=200)):
    return {"url": await email.connections.next_url(state)}


@router.post("/connect/{provider}/complete", response_model=Connection, summary="Finish connecting a mailbox")
async def complete(provider: Provider, body: CompleteRequest, email: Runtime, seller_id: Seller):
    return await email.connections.complete(seller_id, provider, body.transaction_id)


@router.get("/oauth/callback", response_class=HTMLResponse,
            summary="OAuth redirect URI registered with Google and Microsoft")
async def oauth_callback(request: Request, state: str | None = Query(None, max_length=200),
                         code: str | None = Query(None, max_length=4096),
                         error: str | None = Query(None, max_length=200)):
    from .services.connections import callback_page
    email = getattr(request.app.state, "email", None)
    if email is None:
        return HTMLResponse(callback_page(False, "Email is not configured on this server."), status_code=503)
    return HTMLResponse(await email.connections.callback(state, code, error))


# ---- drafts ---------------------------------------------------------------------------------------

@router.post("/internal/draft", response_model=DraftResponse, dependencies=[Depends(internal_caller)],
             summary="Create a draft (HTML body + optional PDF) in the mailbox (AI_python_backend only)")
async def internal_draft(email: Runtime, seller_id: Seller,
                         metadata: str = Form(..., max_length=400_000, description="DraftMetadata as JSON"),
                         attachment: UploadFile | None = File(None, description="The report PDF")):
    try:
        request = DraftMetadata.model_validate(json.loads(metadata))
    except (ValueError, ValidationError) as exc:
        raise EmailError("EMAIL_INVALID_REQUEST", "The draft request is invalid.",
                         details={"errors": _short_errors(exc)}) from None
    file = None
    if attachment is not None:
        limit = email.ctx.settings.MAX_ATTACHMENT_BYTES
        data = await attachment.read(limit + 1)
        if len(data) > limit:
            raise EmailError("EMAIL_ATTACHMENT_TOO_LARGE", details={"limit_bytes": limit})
        file = (attachment.filename or "report.pdf", data)
    return await email.drafts.create(seller_id, request.model_dump(), file)


def _short_errors(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"[:120] for e in exc.errors()[:5]]
    return ["metadata is not valid JSON"]
