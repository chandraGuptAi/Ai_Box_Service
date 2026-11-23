"""Sync engine: runs one queued job (initial / incremental / manual / token refresh).

Job claiming, leases and retries live in PostgreSQL (meta_sync_jobs), so any number of API or
worker processes can run this safely: a job is claimed with ``FOR UPDATE SKIP LOCKED``, the
partial unique index allows only one queued/running job per connection (the sync lock), and a
job whose worker died is re-queued when its lease expires.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.logger import get_logger

from ..context import MetaContext
from ..errors import MetaError
from ..providers.base import AssetInfo, SyncWindow
from ..providers.graph import GraphError

logger = get_logger("module.meta_integration.sync")


def backoff_seconds(retry_count: int, retry_after: int | None = None) -> int:
    base = min(3600, 60 * (2 ** retry_count))
    return max(retry_after or 0, base) + random.randint(0, 30)


class SyncService:
    def __init__(self, ctx: MetaContext) -> None:
        self.ctx = ctx

    async def run_job(self, job: dict[str, Any]) -> str:
        repo = self.ctx.repo
        conn = await repo.get_connection_internal(job["connection_id"])
        if conn is None or conn["connection_status"] != "connected":
            await repo.finish_job(job["id"], "cancelled", 0, "META_NOT_CONNECTED")
            return "cancelled"
        platform = conn["platform"]
        audit = {"seller_id": conn["seller_id"], "connection_id": conn["id"], "platform": platform,
                 "actor": "system"}
        await repo.audit("sync_started", details={"job_id": str(job["id"]), "job_type": job["job_type"]}, **audit)
        records = 0
        try:
            token = await self.ctx.token_for(conn["id"], platform)
            if job["job_type"] == "token_refresh":
                await self._refresh(conn, token)
                await repo.finish_job(job["id"], "succeeded", 0, None)
                await repo.audit("token_refreshed", **audit)
                return "succeeded"
            records = await self._sync(conn, token, job)
        except GraphError as exc:
            return await self._graph_failure(job, conn, exc, records, audit)
        except MetaError as exc:   # e.g. credential missing
            await repo.set_status(conn["id"], "reconnect_required", exc.code)
            await repo.mark_synced(conn["id"], False, exc.code)
            await repo.finish_job(job["id"], "failed", records, exc.code)
            await repo.audit("sync_failed", outcome="failure", details={"error_code": exc.code}, **audit)
            return "failed"
        except (KeyError, ValueError) as exc:   # undecryptable credential (key removed / tampered)
            logger.error("Meta credential for %s cannot be decrypted: %s", conn["id"], type(exc).__name__)
            await repo.set_status(conn["id"], "reconnect_required", "META_RECONNECT_REQUIRED")
            await repo.finish_job(job["id"], "failed", records, "META_RECONNECT_REQUIRED")
            await repo.audit("token_invalidated", outcome="failure", details={"reason": "decrypt_failed"}, **audit)
            return "failed"
        except Exception:   # noqa: BLE001 - never let one job kill the worker
            logger.exception("Meta sync job %s crashed", job["id"])
            return await self._retry_or_fail(job, conn, "META_SYNC_FAILED", records, audit, None)
        await repo.mark_synced(conn["id"], True, None)
        await repo.touch_credential(conn["id"])
        await repo.finish_job(job["id"], "succeeded", records, None)
        await repo.audit("sync_completed", details={"job_id": str(job["id"]), "records": records}, **audit)
        return "succeeded"

    async def _sync(self, conn: dict[str, Any], token: str, job: dict[str, Any]) -> int:
        repo, provider = self.ctx.repo, self.ctx.provider(conn["platform"])
        s = self.ctx.settings
        initial = job["job_type"] == "initial" or conn.get("last_successful_sync_at") is None
        window = SyncWindow.last_days(s.INITIAL_SYNC_DAYS if initial else s.INCREMENTAL_SYNC_DAYS)

        # Health check + asset refresh: also detects Pages/phone numbers removed from the grant.
        stored = await repo.list_assets(conn["id"])
        meta = next((a["metadata"] for a in stored if a["asset_type"] in
                     {"whatsapp_business_account", "instagram_account", "facebook_page"}), {}) or {}
        assets: list[AssetInfo] = await provider.list_assets(token, conn["external_account_id"],
                                                             {"business_id": meta.get("business_portfolio_id"),
                                                              "flow_event": meta.get("signup_flow")})
        await repo.replace_assets_internal(conn["id"], conn["seller_id"], assets)
        asset_ids = {a["external_asset_id"]: a["id"] for a in await repo.list_assets(conn["id"])}

        result = await provider.fetch_insights(token, conn["external_account_id"], assets, window)
        records = await repo.upsert_points(conn["seller_id"], conn["id"], conn["platform"], asset_ids, result.points)
        await repo.upsert_statuses(conn["id"], result.statuses)
        return records

    async def _refresh(self, conn: dict[str, Any], token: str) -> None:
        refreshed = await self.ctx.provider(conn["platform"]).refresh_token(token)
        if refreshed:
            new_token, expires_at = refreshed
            blob, version = self.ctx.encrypt(new_token, str(conn["id"]))
            await self.ctx.repo.update_credential(conn["id"], blob, version, expires_at, refreshed=True)

    async def _graph_failure(self, job, conn, exc: GraphError, records: int, audit: dict) -> str:
        repo = self.ctx.repo
        if exc.kind in {"token_expired", "token_invalid", "permission"}:
            status, code = {"token_expired": ("expired", "META_TOKEN_EXPIRED"),
                            "token_invalid": ("reconnect_required", "META_RECONNECT_REQUIRED"),
                            "permission": ("reconnect_required", "META_PERMISSION_DENIED")}[exc.kind]
            await repo.set_status(conn["id"], status, code)
            await repo.mark_synced(conn["id"], False, code)
            await repo.finish_job(job["id"], "failed", records, code)
            await repo.audit("token_invalidated" if exc.kind != "permission" else "permissions_changed",
                             outcome="failure", details={"meta_code": exc.code, "subcode": exc.subcode}, **audit)
            return "failed"
        code = "META_RATE_LIMITED" if exc.kind == "rate_limited" else "META_SYNC_FAILED"
        return await self._retry_or_fail(job, conn, code, records, audit, exc.retry_after)

    async def _retry_or_fail(self, job, conn, code: str, records: int, audit: dict, retry_after: int | None) -> str:
        repo = self.ctx.repo
        if job["retry_count"] < self.ctx.settings.SYNC_MAX_RETRIES:
            delay = backoff_seconds(job["retry_count"], retry_after)
            await repo.retry_job(job["id"], delay, code, records)
            await repo.audit("sync_retry_scheduled", outcome="failure",
                             details={"error_code": code, "retry_in_seconds": delay}, **audit)
            return "retrying"
        await repo.mark_synced(conn["id"], False, code)
        await repo.finish_job(job["id"], "failed", records, code)
        await repo.audit("sync_failed", outcome="failure", details={"error_code": code}, **audit)
        return "failed"

    # ---- scheduled maintenance ------------------------------------------------------------------

    async def schedule_due(self) -> dict[str, int]:
        repo, s = self.ctx.repo, self.ctx.settings
        out = {"incremental": 0, "token_refresh": 0, "expired": 0}
        for row in await repo.expire_lapsed_credentials():
            out["expired"] += 1
            await repo.audit("token_invalidated", seller_id=row["seller_id"], connection_id=row["id"],
                             platform=row["platform"], actor="system", outcome="failure",
                             details={"reason": "expired"})
        # Instagram tokens last 60 days; refresh those expiring within 10 days (needs >= 24 h age).
        for row in await repo.expiring_credentials(datetime.now(timezone.utc) + timedelta(days=10),
                                                   timedelta(hours=25)):
            _, created = await repo.enqueue_job(row["seller_id"], row["connection_id"], "token_refresh")
            out["token_refresh"] += created
        if s.SYNC_ENABLED:
            for row in await repo.connections_due(s.SYNC_INTERVAL_MINUTES):
                _, created = await repo.enqueue_job(row["seller_id"], row["id"], "incremental")
                out["incremental"] += created
        return out

    async def rotate_keys(self) -> int:
        """Re-encrypt credentials stored under an older key version with the active key."""
        rotated = 0
        while True:
            rows = await self.ctx.repo.credentials_with_old_keys(self.ctx.cipher.active)
            if not rows:
                return rotated
            for row in rows:
                aad = str(row["connection_id"])
                plain = self.ctx.cipher.decrypt(row["encrypted_access_token"], row["key_version"], aad)
                blob, version = self.ctx.encrypt(plain, aad)
                await self.ctx.repo.update_credential(row["connection_id"], blob, version, row["expires_at"],
                                                      refreshed=False)
                rotated += 1
