"""All SQL for the Meta module.

Seller-facing reads and writes always filter on ``seller_id`` (taken from the trusted request
context, never from the request body). Methods named ``*_internal`` are for the worker and
webhook processor, which act on behalf of the system after their own attribution checks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .db import AsyncConnection, AsyncPool

from .providers.base import AccountGrant, AssetInfo, MetricPoint, MetricStatus

ACTIVE_STATES = ("connected", "expired", "reconnect_required", "pending")
_CONN_COLUMNS = """c.id, c.seller_id, c.platform, c.external_account_id, c.external_account_name,
                   c.connection_status, c.permission_scopes, c.connected_at, c.disconnected_at, c.last_sync_at,
                   c.last_successful_sync_at, c.last_error_code, c.created_at, c.updated_at, c.meta_user_id"""


def _uuid(value: str | UUID) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError:
        return None


def now() -> datetime:
    return datetime.now(timezone.utc)


class MetaRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self.pool = pool

    # ---- connections --------------------------------------------------------------------------

    async def list_connections(self, seller_id: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"""
                SELECT {_CONN_COLUMNS},
                       (SELECT j.id FROM meta_sync_jobs j WHERE j.connection_id = c.id
                         AND j.status IN ('queued','running')) AS active_job_id
                FROM meta_connections c
                WHERE c.seller_id = %s AND c.connection_status <> 'disconnected'
                ORDER BY c.platform, c.external_account_name NULLS LAST, c.created_at""", (seller_id,))
            return await cur.fetchall()

    async def get_connection(self, seller_id: str, connection_id: str) -> dict[str, Any] | None:
        cid = _uuid(connection_id)
        if cid is None:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_CONN_COLUMNS} FROM meta_connections c WHERE c.id = %s "
                                     "AND c.seller_id = %s", (cid, seller_id))
            return await cur.fetchone()

    async def get_connection_internal(self, connection_id: UUID) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_CONN_COLUMNS} FROM meta_connections c WHERE c.id = %s",
                                     (connection_id,))
            return await cur.fetchone()

    async def other_sellers_on_account(self, seller_id: str, platform: str, external_id: str) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT count(*) AS n FROM meta_connections WHERE platform = %s
                AND external_account_id = %s AND seller_id <> %s AND connection_status = ANY(%s)""",
                                     (platform, external_id, seller_id, list(ACTIVE_STATES)))
            return (await cur.fetchone())["n"]

    async def save_grant(self, seller_id: str, platform: str, grant: AccountGrant, encrypt) -> tuple[dict, bool]:
        """Create or reconnect a connection with its assets and credential, atomically.

        ``encrypt(token, aad)`` -> (blob, key_version); the AAD is the connection ID.
        Returns (connection row, created).
        """
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(f"""
                INSERT INTO meta_connections AS c (seller_id, platform, external_account_id, external_account_name,
                    meta_user_id, connection_status, permission_scopes, connected_at, disconnected_at,
                    last_error_code)
                VALUES (%s, %s, %s, %s, %s, 'connected', %s, now(), NULL, NULL)
                ON CONFLICT (seller_id, platform, external_account_id) DO UPDATE SET
                    external_account_name = EXCLUDED.external_account_name, meta_user_id = EXCLUDED.meta_user_id,
                    connection_status = 'connected', permission_scopes = EXCLUDED.permission_scopes,
                    connected_at = now(), disconnected_at = NULL, last_error_code = NULL, updated_at = now()
                RETURNING {_CONN_COLUMNS}, (xmax = 0) AS created""",
                                     (seller_id, platform, grant.external_account_id, grant.name,
                                      grant.meta_user_id, grant.scopes))
            row = await cur.fetchone()
            blob, version = encrypt(grant.access_token, str(row["id"]))
            await conn.execute("""
                INSERT INTO meta_credentials (connection_id, encrypted_access_token, key_version, token_type,
                                              expires_at, last_validated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (connection_id) DO UPDATE SET encrypted_access_token = EXCLUDED.encrypted_access_token,
                    key_version = EXCLUDED.key_version, token_type = EXCLUDED.token_type,
                    expires_at = EXCLUDED.expires_at, issued_at = now(), last_validated_at = now(),
                    revoked_at = NULL, updated_at = now()""",
                               (row["id"], blob, version, grant.token_type, grant.expires_at))
            await self._replace_assets(conn, row["id"], seller_id, grant.assets)
            return row, bool(row.pop("created"))

    async def _replace_assets(self, conn: AsyncConnection, connection_id: UUID, seller_id: str,
                              assets: list[AssetInfo]) -> None:
        keep = []
        for asset in assets:
            cur = await conn.execute("""
                INSERT INTO meta_assets (seller_id, connection_id, asset_type, external_asset_id, asset_name, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (connection_id, asset_type, external_asset_id) DO UPDATE SET
                    asset_name = EXCLUDED.asset_name, metadata = EXCLUDED.metadata, updated_at = now()
                RETURNING id""", (seller_id, connection_id, asset.asset_type, asset.external_id, asset.name,
                                  Jsonb(asset.metadata)))
            keep.append((await cur.fetchone())["id"])
        # Assets no longer granted keep their history only if they still have insights; otherwise go.
        await conn.execute("""DELETE FROM meta_assets a WHERE a.connection_id = %s AND NOT (a.id = ANY(%s))
                              AND NOT EXISTS (SELECT 1 FROM meta_insights i WHERE i.asset_id = a.id)""",
                           (connection_id, keep))

    async def replace_assets_internal(self, connection_id: UUID, seller_id: str, assets: list[AssetInfo]) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await self._replace_assets(conn, connection_id, seller_id, assets)

    async def list_assets(self, connection_id: UUID) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT id, asset_type, external_asset_id, asset_name, metadata, created_at,
                                               updated_at FROM meta_assets WHERE connection_id = %s
                                        ORDER BY asset_type, asset_name NULLS LAST""", (connection_id,))
            return await cur.fetchall()

    async def connections_for_asset_internal(self, asset_type: str, external_id: str) -> list[dict[str, Any]]:
        """Connected connections that own this external asset (webhook attribution)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT c.id AS connection_id, c.seller_id, a.id AS asset_id
                FROM meta_assets a JOIN meta_connections c ON c.id = a.connection_id
                WHERE a.asset_type = %s AND a.external_asset_id = %s AND c.connection_status = 'connected'""",
                                     (asset_type, external_id))
            return await cur.fetchall()

    async def connections_for_phone_internal(self, waba_id: str, phone_number_id: str) -> list[dict[str, Any]]:
        """Connected WhatsApp connections where the phone number belongs to that WABA."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT c.id AS connection_id, c.seller_id, p.id AS asset_id
                FROM meta_connections c
                JOIN meta_assets w ON w.connection_id = c.id AND w.asset_type = 'whatsapp_business_account'
                                  AND w.external_asset_id = %s
                JOIN meta_assets p ON p.connection_id = c.id AND p.asset_type = 'whatsapp_phone_number'
                                  AND p.external_asset_id = %s
                WHERE c.platform = 'whatsapp' AND c.connection_status = 'connected'""", (waba_id, phone_number_id))
            return await cur.fetchall()

    async def set_status(self, connection_id: UUID, status: str, error_code: str | None = None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE meta_connections SET connection_status = %s, last_error_code = %s,
                                  updated_at = now() WHERE id = %s AND connection_status <> 'disconnected'""",
                               (status, error_code, connection_id))

    async def mark_synced(self, connection_id: UUID, success: bool, error_code: str | None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE meta_connections SET last_sync_at = now(),
                    last_successful_sync_at = CASE WHEN %s THEN now() ELSE last_successful_sync_at END,
                    last_error_code = %s, updated_at = now() WHERE id = %s""", (success, error_code, connection_id))

    async def disconnect(self, seller_id: str, connection_id: UUID, delete_data: bool) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("""UPDATE meta_connections SET connection_status = 'disconnected',
                    disconnected_at = now(), updated_at = now() WHERE id = %s AND seller_id = %s""",
                               (connection_id, seller_id))
            await self._revoke(conn, connection_id)
            await conn.execute("""UPDATE meta_sync_jobs SET status = 'cancelled', completed_at = now(),
                    updated_at = now() WHERE connection_id = %s AND status IN ('queued','running')""",
                               (connection_id,))
            if delete_data:
                for table in ("meta_insights", "meta_metric_status", "meta_whatsapp_status_events"):
                    await conn.execute(f"DELETE FROM {table} WHERE connection_id = %s", (connection_id,))

    @staticmethod
    async def _revoke(conn: AsyncConnection, connection_id: UUID) -> None:
        await conn.execute("""UPDATE meta_credentials SET encrypted_access_token = NULL, revoked_at = now(),
                              updated_at = now() WHERE connection_id = %s""", (connection_id,))

    async def connections_for_meta_user_internal(self, meta_user_ids: list[str]) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"SELECT {_CONN_COLUMNS} FROM meta_connections c WHERE c.meta_user_id = ANY(%s)"
                                     " AND c.connection_status <> 'disconnected'", (meta_user_ids,))
            return await cur.fetchall()

    async def deauthorize_internal(self, connection_id: UUID, delete_data: bool) -> None:
        row = await self.get_connection_internal(connection_id)
        if row:
            await self.disconnect(row["seller_id"], connection_id, delete_data)

    async def all_connections_admin(self, status: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(f"""SELECT {_CONN_COLUMNS} FROM meta_connections c
                WHERE (%s::text IS NULL OR c.connection_status = %s) ORDER BY c.updated_at DESC LIMIT %s OFFSET %s""",
                                     (status, status, limit, offset))
            return await cur.fetchall()

    # ---- credentials --------------------------------------------------------------------------

    async def get_credential(self, connection_id: UUID) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT connection_id, encrypted_access_token, key_version, token_type,
                expires_at, issued_at, last_validated_at, last_refreshed_at, revoked_at
                FROM meta_credentials WHERE connection_id = %s AND revoked_at IS NULL
                AND encrypted_access_token IS NOT NULL""", (connection_id,))
            return await cur.fetchone()

    async def update_credential(self, connection_id: UUID, blob: bytes, version: str,
                                expires_at: datetime | None, refreshed: bool) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE meta_credentials SET encrypted_access_token = %s, key_version = %s,
                    expires_at = %s, last_refreshed_at = CASE WHEN %s THEN now() ELSE last_refreshed_at END,
                    updated_at = now() WHERE connection_id = %s AND revoked_at IS NULL""",
                               (blob, version, expires_at, refreshed, connection_id))

    async def touch_credential(self, connection_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE meta_credentials SET last_validated_at = now() WHERE connection_id = %s",
                               (connection_id,))

    async def credentials_with_old_keys(self, active_version: str, limit: int = 500) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT connection_id, encrypted_access_token, key_version, expires_at
                FROM meta_credentials WHERE key_version <> %s AND encrypted_access_token IS NOT NULL LIMIT %s""",
                                     (active_version, limit))
            return await cur.fetchall()

    async def expiring_credentials(self, before: datetime, min_age: timedelta) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT c.id AS connection_id, c.seller_id FROM meta_credentials k
                JOIN meta_connections c ON c.id = k.connection_id
                WHERE k.revoked_at IS NULL AND k.expires_at IS NOT NULL AND k.expires_at < %s AND k.expires_at > now()
                  AND COALESCE(k.last_refreshed_at, k.issued_at) < now() - %s
                  AND c.connection_status = 'connected'
                  AND NOT EXISTS (SELECT 1 FROM meta_sync_jobs j WHERE j.connection_id = c.id
                                  AND j.status IN ('queued','running'))""", (before, min_age))
            return await cur.fetchall()

    async def expire_lapsed_credentials(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                UPDATE meta_connections c SET connection_status = 'expired', last_error_code = 'META_TOKEN_EXPIRED',
                       updated_at = now()
                FROM meta_credentials k WHERE k.connection_id = c.id AND k.expires_at IS NOT NULL
                  AND k.expires_at <= now() AND c.connection_status = 'connected'
                RETURNING c.id, c.seller_id, c.platform""")
            return await cur.fetchall()

    # ---- insights -----------------------------------------------------------------------------

    async def upsert_points(self, seller_id: str, connection_id: UUID, platform: str,
                            asset_ids: dict[str, UUID], points: list[MetricPoint]) -> int:
        rows = []
        for p in points:
            asset_id = asset_ids.get(p.asset_external_id)
            if asset_id is None:
                continue
            rows.append((seller_id, connection_id, asset_id, platform, p.metric_name, p.value, p.unit, p.period,
                         p.period_start, p.period_end, p.dimension_key, Jsonb(p.dimensions), p.source))
        if not rows:
            return 0
        async with self.pool.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.executemany("""
                    INSERT INTO meta_insights (seller_id, connection_id, asset_id, platform, metric_name, metric_value,
                        metric_unit, aggregation_period, period_start, period_end, dimension_key, dimensions, source,
                        availability_status, fetched_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s::numeric IS NULL THEN 'not_available' ELSE 'available' END, now())
                    ON CONFLICT (connection_id, asset_id, metric_name, aggregation_period, period_start, dimension_key)
                    DO UPDATE SET metric_value = EXCLUDED.metric_value, period_end = EXCLUDED.period_end,
                        dimensions = EXCLUDED.dimensions, availability_status = EXCLUDED.availability_status,
                        fetched_at = now()""", [(*r, r[5]) for r in rows])
        return len(rows)

    async def upsert_statuses(self, connection_id: UUID, statuses: list[MetricStatus]) -> None:
        if not statuses:
            return
        async with self.pool.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.executemany("""
                    INSERT INTO meta_metric_status (connection_id, metric_name, availability_status, reason, checked_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (connection_id, metric_name) DO UPDATE SET availability_status =
                        EXCLUDED.availability_status, reason = EXCLUDED.reason, checked_at = now()""",
                                      [(connection_id, s.metric_name, s.status, s.reason) for s in statuses])

    async def metric_statuses(self, connection_ids: list[UUID]) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT connection_id, metric_name, availability_status, reason, checked_at
                FROM meta_metric_status WHERE connection_id = ANY(%s)""", (connection_ids,))
            return await cur.fetchall()

    async def report_rows(self, seller_id: str, connection_ids: list[UUID], start: datetime,
                          end: datetime) -> list[dict[str, Any]]:
        """Insights rows of these (seller-owned) connections that overlap [start, end)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT i.connection_id, i.metric_name, i.metric_value, i.metric_unit, i.aggregation_period,
                       i.period_start, i.period_end, i.dimensions, i.source, i.fetched_at, a.external_asset_id,
                       a.asset_name, a.asset_type
                FROM meta_insights i JOIN meta_assets a ON a.id = i.asset_id
                WHERE i.seller_id = %s AND i.connection_id = ANY(%s)
                  AND ((i.aggregation_period IN ('day', 'lifetime') AND i.period_start >= %s AND i.period_start < %s)
                       OR i.aggregation_period = 'snapshot')
                ORDER BY i.metric_name, i.period_start""", (seller_id, connection_ids, start, end))
            return await cur.fetchall()

    async def whatsapp_status_counts(self, seller_id: str, connection_ids: list[UUID], start: datetime,
                                     end: datetime) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT connection_id, status, date_trunc('day', status_at AT TIME ZONE 'UTC') AS day, count(*) AS n
                FROM meta_whatsapp_status_events
                WHERE seller_id = %s AND connection_id = ANY(%s) AND status_at >= %s AND status_at < %s
                  AND status IN ('read', 'failed')
                GROUP BY connection_id, status, day ORDER BY day""", (seller_id, connection_ids, start, end))
            return await cur.fetchall()

    # ---- sync jobs ----------------------------------------------------------------------------

    async def enqueue_job(self, seller_id: str, connection_id: UUID, job_type: str,
                          run_after: datetime | None = None) -> tuple[dict[str, Any], bool]:
        """Queue a job unless one is already queued/running for the connection (sync lock).

        Returns (job, created)."""
        async with self.pool.connection() as conn:
            try:
                async with conn.transaction():
                    cur = await conn.execute("""INSERT INTO meta_sync_jobs (seller_id, connection_id, job_type,
                        run_after) VALUES (%s, %s, %s, COALESCE(%s, now())) RETURNING *""",
                                             (seller_id, connection_id, job_type, run_after))
                    return await cur.fetchone(), True
            except UniqueViolation:
                cur = await conn.execute("""SELECT * FROM meta_sync_jobs WHERE connection_id = %s
                                            AND status IN ('queued','running')""", (connection_id,))
                return await cur.fetchone(), False

    async def claim_job(self, lease_seconds: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute("""
                UPDATE meta_sync_jobs j SET status = 'running', lease_until = now() + make_interval(secs => %s),
                       started_at = COALESCE(j.started_at, now()), updated_at = now()
                WHERE j.id = (SELECT id FROM meta_sync_jobs WHERE status = 'queued' AND run_after <= now()
                              ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
                RETURNING j.*""", (lease_seconds,))
            return await cur.fetchone()

    async def finish_job(self, job_id: UUID, status: str, records: int, error_code: str | None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE meta_sync_jobs SET status = %s, records_processed = records_processed + %s,
                last_error = %s, completed_at = now(), lease_until = NULL, updated_at = now()
                WHERE id = %s AND status = 'running'""", (status, records, error_code, job_id))

    async def retry_job(self, job_id: UUID, delay_seconds: int, error_code: str, records: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE meta_sync_jobs SET status = 'queued', retry_count = retry_count + 1,
                run_after = now() + make_interval(secs => %s), last_error = %s, lease_until = NULL,
                records_processed = records_processed + %s, updated_at = now()
                WHERE id = %s AND status = 'running'""", (delay_seconds, error_code, records, job_id))

    async def recover_expired_leases(self) -> int:
        """Jobs whose worker died mid-run go back to the queue."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("""UPDATE meta_sync_jobs SET status = 'queued', lease_until = NULL,
                retry_count = retry_count + 1, last_error = 'META_WORKER_LEASE_EXPIRED', updated_at = now()
                WHERE status = 'running' AND lease_until < now()""")
            return cur.rowcount

    async def get_job(self, seller_id: str, job_id: str) -> dict[str, Any] | None:
        jid = _uuid(job_id)
        if jid is None:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT j.*, c.platform FROM meta_sync_jobs j
                JOIN meta_connections c ON c.id = j.connection_id WHERE j.id = %s AND j.seller_id = %s""",
                                     (jid, seller_id))
            return await cur.fetchone()

    async def last_manual_job(self, connection_id: UUID) -> datetime | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT max(created_at) AS t FROM meta_sync_jobs WHERE connection_id = %s
                                        AND job_type = 'manual'""", (connection_id,))
            return (await cur.fetchone())["t"]

    async def connections_due(self, interval_minutes: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                SELECT c.id, c.seller_id FROM meta_connections c
                WHERE c.connection_status = 'connected'
                  AND (c.last_sync_at IS NULL OR c.last_sync_at < now() - make_interval(mins => %s))
                  AND NOT EXISTS (SELECT 1 FROM meta_sync_jobs j WHERE j.connection_id = c.id
                                  AND j.status IN ('queued','running'))
                LIMIT 200""", (interval_minutes,))
            return await cur.fetchall()

    # ---- webhooks -----------------------------------------------------------------------------

    async def insert_webhook_events(self, events: list[dict[str, Any]]) -> int:
        inserted = 0
        async with self.pool.connection() as conn, conn.transaction():
            for e in events:
                cur = await conn.execute("""
                    INSERT INTO meta_webhook_events (platform, event_id, external_account_id, event_type, payload)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (platform, event_id) DO NOTHING""",
                                         (e["platform"], e["event_id"], e.get("external_account_id"),
                                          e.get("event_type"), Jsonb(e["payload"])))
                inserted += cur.rowcount
        return inserted

    async def claim_webhook_events(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute("""
                UPDATE meta_webhook_events e SET processing_status = 'processing', attempts = attempts + 1
                WHERE e.id IN (SELECT id FROM meta_webhook_events
                               WHERE processing_status = 'pending'
                                  OR (processing_status = 'processing' AND received_at < now() - interval '10 minutes')
                                  OR (processing_status = 'failed' AND attempts < 5)
                               ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT %s)
                RETURNING e.*""", (limit,))
            return await cur.fetchall()

    async def mark_webhook_event(self, event_id: int, status: str, error: str | None = None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""UPDATE meta_webhook_events SET processing_status = %s, last_error = %s,
                processed_at = now() WHERE id = %s""", (status, error, event_id))

    async def insert_status_events(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        async with self.pool.connection() as conn, conn.transaction():
            async with conn.cursor() as cur:
                await cur.executemany("""
                    INSERT INTO meta_whatsapp_status_events (connection_id, seller_id, phone_number_id, message_ref,
                        status, status_at, pricing_category) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (connection_id, message_ref, status) DO NOTHING""", rows)
        return len(rows)

    # ---- OAuth transactions --------------------------------------------------------------------

    async def create_transaction(self, seller_id: str, platform: str, state_hash: str, ttl_seconds: int,
                                 encrypted_verifier: bytes | None, key_version: str | None) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                INSERT INTO meta_oauth_transactions (seller_id, platform, state_hash, encrypted_verifier, key_version,
                    expires_at) VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                RETURNING id, expires_at""", (seller_id, platform, state_hash, encrypted_verifier, key_version,
                                              ttl_seconds))
            return await cur.fetchone()

    async def authorize_transaction(self, state_hash: str, encrypted_code: bytes | None, key_version: str | None,
                                    denied: bool) -> dict[str, Any] | None:
        """Callback: attach the code to a pending, unexpired transaction (one-time)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                UPDATE meta_oauth_transactions SET status = %s, encrypted_code = %s, key_version = COALESCE(%s,
                    key_version), authorized_at = now()
                WHERE state_hash = %s AND status = 'pending' AND expires_at > now()
                RETURNING id, seller_id, platform""",
                                     ("denied" if denied else "authorized", encrypted_code, key_version, state_hash))
            return await cur.fetchone()

    async def consume_transaction(self, seller_id: str, transaction_id: str, platform: str,
                                  from_status: str) -> dict[str, Any] | None:
        """Atomically consume a transaction owned by this seller. None = invalid/expired/used."""
        tid = _uuid(transaction_id)
        if tid is None:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute("""
                UPDATE meta_oauth_transactions SET status = 'consumed', consumed_at = now()
                WHERE id = %s AND seller_id = %s AND platform = %s AND status = %s AND expires_at > now()
                RETURNING id, state_hash, encrypted_code, encrypted_verifier, key_version""",
                                     (tid, seller_id, platform, from_status))
            return await cur.fetchone()

    async def transaction_status(self, seller_id: str, transaction_id: str) -> dict[str, Any] | None:
        tid = _uuid(transaction_id)
        if tid is None:
            return None
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT id, platform, status, expires_at FROM meta_oauth_transactions
                                        WHERE id = %s AND seller_id = %s""", (tid, seller_id))
            return await cur.fetchone()

    # ---- audit + retention --------------------------------------------------------------------

    async def audit(self, action: str, *, seller_id: str | None = None, connection_id: UUID | None = None,
                    platform: str | None = None, outcome: str = "success", actor: str = "seller",
                    details: dict[str, Any] | None = None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""INSERT INTO meta_audit_logs (seller_id, connection_id, platform, action, outcome,
                actor, details) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                               (seller_id, connection_id, platform, action, outcome, actor, Jsonb(details or {})))

    async def audit_logs(self, seller_id: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT id, seller_id, connection_id, platform, action, outcome, actor, details,
                created_at FROM meta_audit_logs WHERE (%s::text IS NULL OR seller_id = %s)
                ORDER BY created_at DESC LIMIT %s OFFSET %s""", (seller_id, seller_id, limit, offset))
            return await cur.fetchall()

    async def record_deletion(self, code: str, meta_user_hash: str, connections: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("""INSERT INTO meta_data_deletion_requests (confirmation_code, meta_user_hash,
                connections) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""", (code, meta_user_hash, connections))

    async def deletion_status(self, code: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("""SELECT confirmation_code, status, created_at FROM meta_data_deletion_requests
                                        WHERE confirmation_code = %s""", (code,))
            return await cur.fetchone()

    async def purge(self, webhook_days: int, audit_days: int) -> dict[str, int]:
        out = {}
        async with self.pool.connection() as conn, conn.transaction():
            for name, sql, args in (
                ("webhook_events", "DELETE FROM meta_webhook_events WHERE received_at < now() - make_interval(days => %s)"
                 " AND processing_status IN ('processed','unmatched','ignored','failed')", (webhook_days,)),
                ("status_events", "DELETE FROM meta_whatsapp_status_events WHERE status_at < now() - "
                 "make_interval(days => %s)", (max(webhook_days, 400),)),
                ("oauth_transactions", "DELETE FROM meta_oauth_transactions WHERE expires_at < now() - "
                 "interval '1 day'", ()),
                ("audit_logs", "DELETE FROM meta_audit_logs WHERE created_at < now() - make_interval(days => %s)",
                 (audit_days,)),
                ("sync_jobs", "DELETE FROM meta_sync_jobs WHERE status IN ('succeeded','partial','failed','cancelled')"
                 " AND created_at < now() - interval '30 days'", ()),
            ):
                out[name] = (await conn.execute(sql, args)).rowcount
        return out
