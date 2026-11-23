"""Background worker: sync jobs, webhook processing, scheduling and retention.

Runs inside the API process by default (META_WORKER_ENABLED=true). To run it separately:

    python -m app.modules.meta_integration.worker

Several workers may run at once; PostgreSQL row locking keeps each job/event single-owner.
"""
from __future__ import annotations

import asyncio
import time

from app.core.logger import get_logger

from .context import MetaContext
from .services.sync import SyncService
from .services.webhooks import WebhookService

logger = get_logger("module.meta_integration.worker")

SCHEDULE_EVERY = 60          # seconds between scheduling passes
PURGE_EVERY = 6 * 3600
JOBS_PER_TICK = 5


class MetaWorker:
    def __init__(self, ctx: MetaContext) -> None:
        self.ctx = ctx
        self.sync = SyncService(ctx)
        self.webhooks = WebhookService(ctx)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_schedule = 0.0
        self._last_purge = 0.0

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="meta-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=30)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _loop(self) -> None:
        logger.info("Meta worker started")
        while not self._stop.is_set():
            try:
                busy = await self.run_once()
            except Exception:   # noqa: BLE001 - keep the loop alive (DB restarts etc.)
                logger.exception("Meta worker tick failed")
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.ctx.settings.WORKER_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
        logger.info("Meta worker stopped")

    async def run_once(self) -> bool:
        """One tick. Returns True if work was found (the loop then continues without sleeping)."""
        now = time.monotonic()
        if now - self._last_schedule >= SCHEDULE_EVERY:
            self._last_schedule = now
            await self.ctx.repo.recover_expired_leases()
            scheduled = await self.sync.schedule_due()
            if any(scheduled.values()):
                logger.info("Meta scheduling: %s", scheduled)
        if now - self._last_purge >= PURGE_EVERY:
            self._last_purge = now
            s = self.ctx.settings
            logger.info("Meta retention purge: %s", await self.ctx.repo.purge(s.WEBHOOK_RETENTION_DAYS,
                                                                             s.AUDIT_RETENTION_DAYS))
        events = await self.webhooks.process_batch()
        jobs = 0
        while jobs < JOBS_PER_TICK and not self._stop.is_set():
            job = await self.ctx.repo.claim_job(self.ctx.settings.SYNC_LEASE_SECONDS)
            if job is None:
                break
            jobs += 1
            await self.sync.run_job(job)
        return bool(events or jobs)


async def _main() -> None:   # pragma: no cover - process entry point
    from app.core.logger import setup_logging

    from .module import build_context

    setup_logging()
    ctx = await build_context()
    if ctx is None:
        raise SystemExit("Meta integration is not configured (see META_INTEGRATION.md)")
    worker = MetaWorker(ctx)
    worker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await worker.stop()
        await ctx.graph.close()
        await ctx.repo.pool.close()


if __name__ == "__main__":   # pragma: no cover
    asyncio.run(_main())
