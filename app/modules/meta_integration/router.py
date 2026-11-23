"""HTTP API under /api/meta.

Seller endpoints identify the seller by asking BigShip whose login token (X-Shipment-Authorization)
the request carries (see identity.py); X-User-Id is only trusted with META_SELLER_IDENTITY=header in
local development. A seller ID in a body, query or header is never proof of ownership. Meta-facing
endpoints (OAuth callback, webhooks, deauthorize, data deletion) are authenticated by OAuth state or
Meta signatures instead.
"""
from __future__ import annotations

import secrets
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Form, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.routing import APIRoute

from app.core.logger import get_logger

from .errors import MetaError, error_responses
from .schemas import (CompleteRequest, CompleteResponse, ConnectionDetail, ConnectionsResponse, DisconnectResponse,
                      Platform, PlatformsResponse, ReportContextRequest, ReportContextResponse, StartResponse,
                      SyncJob, TransactionStatus)
from .security import parse_signed_request, random_token, sha256_hex
from .config import MetaSettings
from .services.connections import describe_platforms, job_view

logger = get_logger("module.meta_integration.api")
MAX_WEBHOOK_BYTES = 1024 * 1024


class MetaRoute(APIRoute):
    """Turns MetaError into the structured error body on every route of this module."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except MetaError as exc:
                return exc.response()
        return route


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(route_class=MetaRoute, dependencies=[Depends(_no_store)])


def runtime(request: Request):
    meta = getattr(request.app.state, "meta", None)
    if meta is None:
        raise MetaError("META_PLATFORM_NOT_CONFIGURED", "Meta integration is not configured on this server.")
    return meta


async def seller(
    meta=Depends(runtime),
    x_shipment_authorization: Annotated[str | None, Header(
        alias="X-Shipment-Authorization", max_length=2100,
        description="The seller's own BigShip login (`Bearer <token>`). BigShip confirms whose it is; "
                    "required unless META_SELLER_IDENTITY=header (local development).")] = None,
    x_user_id: Annotated[str | None, Header(max_length=200, description="Ignored unless "
                                            "META_SELLER_IDENTITY=header")] = None,
) -> str:
    return await meta.identity.resolve(x_shipment_authorization, x_user_id)


def admin(x_meta_admin_key: Annotated[str | None, Header()] = None, meta=Depends(runtime)) -> None:
    expected = meta.ctx.settings.ADMIN_API_KEY
    if not expected:
        raise MetaError("META_PLATFORM_NOT_CONFIGURED", "The Meta admin API is disabled (META_ADMIN_API_KEY).")
    if not x_meta_admin_key or not secrets.compare_digest(x_meta_admin_key.encode(), expected.encode()):
        raise MetaError("META_PERMISSION_DENIED", "A valid X-Meta-Admin-Key header is required.")


Runtime = Annotated[Any, Depends(runtime)]
Seller = Annotated[str, Depends(seller)]
ConnectionId = Annotated[str, Path(pattern=r"^[0-9a-fA-F-]{36}$")]


# ---- platforms & connections --------------------------------------------------------------------

@router.get("/platforms", response_model=PlatformsResponse, summary="Supported Meta platforms and readiness")
async def platforms(request: Request,
                    x_shipment_authorization: Annotated[str | None, Header(alias="X-Shipment-Authorization",
                                                                           max_length=2100)] = None,
                    x_user_id: Annotated[str | None, Header(max_length=200)] = None):
    meta = getattr(request.app.state, "meta", None)
    settings = meta.ctx.settings if meta else MetaSettings()
    platforms = describe_platforms(settings)
    if meta is None:   # database/keys missing: nothing can be connected yet
        for p in platforms:
            p["configured"] = False
    seller_verified = False
    if meta is not None and (x_shipment_authorization or x_user_id):
        # Best effort: readiness never fails because the seller could not be verified.
        try:
            seller_id = await meta.identity.resolve(x_shipment_authorization, x_user_id)
            rows = await meta.connections.list(seller_id)
        except MetaError:
            rows = None
        if rows is not None:
            seller_verified = True
            for p in platforms:
                status = _best_status([r["connection_status"] for r in rows if r["platform"] == p["platform"]])
                p["connection_status"], p["seller_connected"] = status, status == "connected"
    return {"platforms": platforms, "webhook_configured": bool(meta) and settings.webhook_configured(),
            "seller_verified": seller_verified}


_STATUS_RANK = ("connected", "reconnect_required", "expired", "failed", "pending")


def _best_status(statuses: list[str]) -> str:
    return next((s for s in _STATUS_RANK if s in statuses), "not_connected")


@router.get("/connections", response_model=ConnectionsResponse, summary="The seller's Meta connections",
            responses=error_responses("META_PLATFORM_NOT_CONFIGURED"))
async def list_connections(meta: Runtime, seller_id: Seller):
    return {"connections": await meta.connections.list(seller_id)}


@router.get("/connections/{connection_id}", response_model=ConnectionDetail, summary="Connection details and assets",
            responses=error_responses("META_CONNECTION_NOT_FOUND"))
async def connection_detail(connection_id: ConnectionId, meta: Runtime, seller_id: Seller):
    return await meta.connections.detail(seller_id, connection_id)


@router.delete("/connections/{connection_id}", response_model=DisconnectResponse, summary="Disconnect an account",
               responses=error_responses("META_CONNECTION_NOT_FOUND"))
async def disconnect(connection_id: ConnectionId, meta: Runtime, seller_id: Seller,
                     delete_data: bool = Query(False, description="Also delete the synced metrics now")):
    return await meta.connections.disconnect(seller_id, connection_id, delete_data)


@router.post("/connections/{connection_id}/sync", response_model=SyncJob, status_code=202,
             summary="Queue a manual sync (returns the queued/running job)",
             responses=error_responses("META_RECONNECT_REQUIRED", "META_SELLER_RATE_LIMITED",
                                       "META_CONNECTION_NOT_FOUND"))
async def request_sync(connection_id: ConnectionId, meta: Runtime, seller_id: Seller, response: Response):
    job, created = await meta.connections.request_sync(seller_id, connection_id)
    if not created:
        response.headers["X-Meta-Sync-Existing"] = "true"
    return job_view(job)


@router.get("/sync-jobs/{job_id}", response_model=SyncJob, summary="Sync job status",
            responses=error_responses("META_JOB_NOT_FOUND"))
async def sync_job(job_id: ConnectionId, meta: Runtime, seller_id: Seller):
    return job_view(await meta.connections.job(seller_id, job_id))


# ---- connect flows --------------------------------------------------------------------------------

@router.post("/connect/{platform}/start", response_model=StartResponse, response_model_exclude_none=True,
             summary="Begin connecting a Meta account",
             responses=error_responses("META_PLATFORM_NOT_CONFIGURED", "META_SELLER_RATE_LIMITED"))
async def start(platform: Platform, meta: Runtime, seller_id: Seller):
    return await meta.connections.start(seller_id, platform)


@router.post("/connect/{platform}/complete", response_model=CompleteResponse, summary="Finish connecting",
             responses=error_responses("META_TRANSACTION_INVALID", "META_AUTHORIZATION_FAILED",
                                       "META_PERMISSION_DENIED", "META_ACCOUNT_NOT_ELIGIBLE",
                                       "META_ACCOUNT_CONNECTED_ELSEWHERE", "META_RATE_LIMITED"))
async def complete(platform: Platform, body: CompleteRequest, meta: Runtime, seller_id: Seller):
    if platform == "whatsapp" and not (body.code and body.waba_id):
        raise MetaError("META_INVALID_REQUEST", "WhatsApp signup needs `code` and `waba_id`.", platform=platform)
    return await meta.connections.complete(seller_id, platform, body.model_dump())


@router.get("/connect/transactions/{transaction_id}", response_model=TransactionStatus,
            summary="Status of a connection attempt (after the OAuth redirect)")
async def transaction(transaction_id: ConnectionId, meta: Runtime, seller_id: Seller):
    return await meta.connections.transaction_status(seller_id, transaction_id)


@router.get("/oauth/callback", include_in_schema=True, summary="OAuth redirect target registered with Meta",
            response_class=RedirectResponse, status_code=302)
async def oauth_callback(request: Request, state: str | None = Query(None, max_length=200),
                         code: str | None = Query(None, max_length=2048), error: str | None = Query(None, max_length=200)):
    meta = getattr(request.app.state, "meta", None)
    if meta is None:
        return PlainTextResponse("Meta integration is not configured.", status_code=503)
    return RedirectResponse(await meta.connections.callback(state, code, error), status_code=302)


# ---- reporting ------------------------------------------------------------------------------------

@router.post("/report-context", response_model=ReportContextResponse,
             summary="Synchronized Meta metrics for an AI Box report (data source adapter)")
async def report_context(body: ReportContextRequest, meta: Runtime, seller_id: Seller):
    return await meta.reporting.context(seller_id, body.data_sources, body.from_date, body.to_date, body.timezone)


# ---- webhooks -------------------------------------------------------------------------------------

@router.get("/webhook", response_class=PlainTextResponse, summary="Meta webhook verification challenge")
async def webhook_verify(request: Request, mode: str = Query("", alias="hub.mode"),
                         token: str = Query("", alias="hub.verify_token"),
                         challenge: str = Query("", alias="hub.challenge", max_length=200)):
    meta = getattr(request.app.state, "meta", None)
    expected = meta.ctx.settings.WEBHOOK_VERIFY_TOKEN if meta else ""
    if mode == "subscribe" and expected and secrets.compare_digest(token.encode(), expected.encode()):
        return PlainTextResponse(challenge)
    return PlainTextResponse("Forbidden", status_code=403)


@router.post("/webhook", summary="Meta webhook events (X-Hub-Signature-256 required)")
async def webhook_receive(request: Request, x_hub_signature_256: Annotated[str | None, Header()] = None):
    meta = getattr(request.app.state, "meta", None)
    if meta is None or not meta.ctx.settings.webhook_configured():
        return JSONResponse({"detail": "not configured"}, status_code=503)
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_WEBHOOK_BYTES:
        return JSONResponse({"detail": "too large"}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        return JSONResponse({"detail": "too large"}, status_code=413)
    if not meta.webhooks.verify_signature(raw, x_hub_signature_256):
        logger.warning("Meta webhook rejected: invalid signature")
        return JSONResponse({"detail": "invalid signature"}, status_code=401)
    events, stored = await meta.webhooks.ingest(raw)
    return {"received": events, "stored": stored}


# ---- Meta app callbacks (signed_request) ------------------------------------------------------------

def _signed(meta, signed_request: str) -> dict[str, Any]:
    s = meta.ctx.settings
    for secret in filter(None, (s.APP_SECRET, s.INSTAGRAM_APP_SECRET)):
        data = parse_signed_request(signed_request, secret)
        if data and data.get("user_id"):
            return data
    raise MetaError("META_AUTHORIZATION_FAILED", "Invalid signed_request.")


@router.post("/deauthorize", summary="Meta 'Deauthorize callback URL' (user removed the app)")
async def deauthorize(meta: Runtime, signed_request: str = Form(..., max_length=4096)):
    data = _signed(meta, signed_request)
    count = await meta.connections.deauthorize_meta_user(str(data["user_id"]), False, "account_deauthorized")
    return {"success": True, "connections": count}


@router.post("/data-deletion", summary="Meta 'Data deletion request URL'")
async def data_deletion(request: Request, meta: Runtime, signed_request: str = Form(..., max_length=4096)):
    data = _signed(meta, signed_request)
    user = str(data["user_id"])
    count = await meta.connections.deauthorize_meta_user(user, True, "data_deletion_requested")
    code = random_token(12)
    await meta.ctx.repo.record_deletion(code, sha256_hex(user), count)
    public = meta.ctx.settings.PUBLIC_API_BASE_URL.rstrip("/")
    status_url = f"{public}/data-deletion/{code}" if public else str(request.url_for("data_deletion_status",
                                                                                     code=code))
    return {"url": status_url, "confirmation_code": code}


@router.get("/data-deletion/{code}", name="data_deletion_status", summary="Data deletion request status")
async def data_deletion_status(meta: Runtime, code: str = Path(pattern=r"^[A-Za-z0-9_-]{8,40}$")):
    row = await meta.ctx.repo.deletion_status(code)
    if row is None:
        raise MetaError("META_INVALID_REQUEST", "Unknown confirmation code.")
    return {"confirmation_code": row["confirmation_code"], "status": row["status"], "requested_at": row["created_at"]}


# ---- admin ----------------------------------------------------------------------------------------

admin_router = APIRouter(prefix="/admin", route_class=MetaRoute, dependencies=[Depends(admin)])


@admin_router.get("/connections", summary="All sellers' connections (no secrets)")
async def admin_connections(meta: Runtime, status: str | None = Query(None, max_length=30),
                            limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    rows = await meta.ctx.repo.all_connections_admin(status, limit, offset)
    return {"connections": [{**meta.connections.summary(r), "seller_id": r["seller_id"]} for r in rows]}


@admin_router.get("/audit-logs", summary="Meta audit trail")
async def admin_audit(meta: Runtime, seller_id: str | None = Query(None, max_length=200),
                      limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    return {"logs": await meta.ctx.repo.audit_logs(seller_id, limit, offset)}


@admin_router.post("/rotate-keys", summary="Re-encrypt stored tokens with the active key version")
async def admin_rotate(meta: Runtime):
    rotated = await meta.sync.rotate_keys()
    await meta.ctx.repo.audit("encryption_keys_rotated", actor="admin", details={"credentials": rotated})
    return {"rotated": rotated, "active_key_version": meta.ctx.cipher.active}


router.include_router(admin_router)
