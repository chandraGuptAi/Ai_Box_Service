"""Meta integration: WhatsApp Business, Instagram professional and Facebook Page connections per seller,
background sync of their insights into PostgreSQL, and a report data source for AI Box.

A missing or broken Meta configuration never stops the service: the module logs why, keeps
``app.state.meta = None`` and its endpoints answer META_PLATFORM_NOT_CONFIGURED, while every other
module (shipments, reports, attachments, ...) keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import APIRouter

from app.core.config import get_settings
from app.core.module_base import BaseModule

from .config import MetaSettings
from .context import MetaContext
from .db import migrate, open_pool
from .identity import SellerIdentity
from .providers import GraphClient
from .repository import MetaRepository
from .router import router
from .services import ConnectionService, ReportingService, SyncService, WebhookService
from .worker import MetaWorker

#: Tests replace these with an httpx.MockTransport; production uses the network.
GRAPH_TRANSPORT: httpx.AsyncBaseTransport | None = None
BIGSHIP_TRANSPORT: httpx.AsyncBaseTransport | None = None


@dataclass
class MetaRuntime:
    ctx: MetaContext
    connections: ConnectionService
    sync: SyncService
    webhooks: WebhookService
    reporting: ReportingService
    identity: SellerIdentity
    worker: MetaWorker | None = None


def startup_problems(settings: MetaSettings) -> list[str]:
    problems = []
    if not settings.database_configured:
        problems.append("DATABASE_URL (the AI_python_backend database) is empty")
    try:
        keys = settings.encryption_keys
        if not keys or settings.TOKEN_ENCRYPTION_ACTIVE_KEY not in keys:
            problems.append("META_TOKEN_ENCRYPTION_KEYS / META_TOKEN_ENCRYPTION_ACTIVE_KEY are not set")
    except (ValueError, TypeError) as exc:
        problems.append(f"META_TOKEN_ENCRYPTION_KEYS is invalid ({exc})")
    app_settings = get_settings()
    if app_settings.ENV == "production" and not app_settings.SERVICE_TOKEN:
        # Seller identity (X-User-Id) is only trustworthy when the gateway is the only caller.
        problems.append("SERVICE_TOKEN must be set in production (X-User-Id is trusted only from the gateway)")
    return problems


async def build_context(settings: MetaSettings | None = None) -> MetaContext | None:
    settings = settings or MetaSettings()
    if startup_problems(settings):
        return None
    pool = await open_pool(settings.DATABASE_URL, settings.DB_POOL_MIN, settings.DB_POOL_MAX)
    try:
        async with pool.connection() as conn:
            await migrate(conn)
    except Exception:
        await pool.close()
        raise
    graph = GraphClient(settings.GRAPH_API_VERSION or "v0.0", settings.APP_SECRET,
                        instagram_app_secret=settings.INSTAGRAM_APP_SECRET, timeout=settings.HTTP_TIMEOUT_SECONDS,
                        transport=GRAPH_TRANSPORT)
    return MetaContext.build(settings, MetaRepository(pool), graph)


class Module(BaseModule):
    name = "meta_integration"
    title = "Meta Integration"
    description = "Seller-owned WhatsApp Business, Instagram and Facebook Page connections and insights."
    depends_on: list[str] = []

    runtime: MetaRuntime | None = None

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        app.state.meta = None
        settings = MetaSettings()
        problems = startup_problems(settings)
        if problems:
            self.logger.warning("Meta integration disabled: %s", "; ".join(problems))
            return
        try:
            ctx = await build_context(settings)
        except Exception as exc:   # noqa: BLE001 - DB down must not stop shipment reporting
            self.logger.error("Meta integration disabled: database unavailable (%s)", type(exc).__name__)
            return
        connections = ConnectionService(ctx)
        runtime = MetaRuntime(ctx, connections, SyncService(ctx), WebhookService(ctx),
                              ReportingService(ctx, connections), SellerIdentity(settings, BIGSHIP_TRANSPORT))
        identity_problems = settings.identity_problems()
        if identity_problems:
            self.logger.warning("Meta seller routes disabled until configured: %s", "; ".join(identity_problems))
        if settings.WORKER_ENABLED:
            runtime.worker = MetaWorker(ctx)
            runtime.worker.start()
        self.runtime = app.state.meta = runtime
        configured = [p for p in settings.ENABLED_PLATFORMS if not settings.platform_problems(p)]
        self.logger.info("started (Graph %s, platforms ready: %s, worker=%s)", settings.GRAPH_API_VERSION or "unset",
                         configured or "none", settings.WORKER_ENABLED)

    async def on_shutdown(self, app) -> None:
        runtime, self.runtime = self.runtime, None
        app.state.meta = None
        if runtime is None:
            return
        if runtime.worker:
            await runtime.worker.stop()
        await runtime.identity.close()
        await runtime.ctx.graph.close()
        await runtime.ctx.repo.pool.close()
