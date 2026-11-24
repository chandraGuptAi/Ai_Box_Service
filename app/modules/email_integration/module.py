"""Email integration: sellers connect their own Gmail or Outlook mailbox, and AI Box puts a report email
(AI-written body + PDF) there as a draft, opened in a new tab for the user to finish and send.

A missing configuration or database never stops the service: the module logs why, keeps
``app.state.email = None`` and its endpoints answer EMAIL_NOT_CONFIGURED.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import APIRouter

from app.core.module_base import BaseModule
from app.core.pg import migrate, open_pool

from .config import PROVIDERS, EmailSettings
from .context import EmailContext
from .repository import EmailRepository
from .router import router
from .services import ConnectionService, DraftService

#: Tests replace these with an httpx.MockTransport; production uses the network.
PROVIDER_TRANSPORT: httpx.AsyncBaseTransport | None = None
BIGSHIP_TRANSPORT: httpx.AsyncBaseTransport | None = None

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x454D4149  # "EMAI"


@dataclass
class EmailRuntime:
    ctx: EmailContext
    connections: ConnectionService
    drafts: DraftService


async def build_runtime(settings: EmailSettings) -> EmailRuntime:
    pool = await open_pool(settings.DATABASE_URL, "email_integration", settings.DB_POOL_MIN, settings.DB_POOL_MAX)
    try:
        async with pool.connection() as conn:
            await migrate(conn, MIGRATIONS_DIR, "email_schema_migrations", _LOCK_KEY)
    except Exception:
        await pool.close()
        raise
    ctx = EmailContext.build(settings, EmailRepository(pool), PROVIDER_TRANSPORT, BIGSHIP_TRANSPORT)
    connections = ConnectionService(ctx)
    return EmailRuntime(ctx, connections, DraftService(ctx, connections))


class Module(BaseModule):
    name = "email_integration"
    title = "Email Integration"
    description = "Seller-owned Gmail / Outlook mailboxes: AI Box report emails as ready-to-send drafts."

    runtime: EmailRuntime | None = None

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        app.state.email = None
        settings = EmailSettings()
        problems = settings.startup_problems()
        if problems:
            self.logger.warning("Email integration disabled: %s", "; ".join(problems))
            return
        try:
            runtime = await build_runtime(settings)
        except Exception as exc:   # noqa: BLE001 - a DB outage must not stop the other modules
            self.logger.error("Email integration disabled: database unavailable (%s)", type(exc).__name__)
            return
        self.runtime = app.state.email = runtime
        ready = [p for p in PROVIDERS if not settings.provider_problems(p)]
        if not settings.INTERNAL_KEY:
            self.logger.warning("EMAIL_INTERNAL_KEY is empty: report drafts cannot be created until it is set")
        self.logger.info("started (providers ready: %s)", ready or "none")

    async def on_shutdown(self, app) -> None:
        runtime, self.runtime = self.runtime, None
        app.state.email = None
        if runtime is not None:
            await runtime.ctx.close()
