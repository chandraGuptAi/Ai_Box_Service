"""Every SQL statement of the email module. Seller-facing queries always filter on ``seller_id``."""
from __future__ import annotations

import json
from typing import Any

from app.core.pg import AsyncPool

_CONNECTION_COLUMNS = ("id, seller_id, provider, account_email, display_name, status, scopes, connected_at, "
                       "disconnected_at, last_used_at, last_error_code, updated_at")


class EmailRepository:
    def __init__(self, pool: AsyncPool) -> None:
        self.pool = pool

    async def _one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchone()

    async def _all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()

    # ---- connections ------------------------------------------------------------------------------

    async def list_connections(self, seller_id: str) -> list[dict[str, Any]]:
        return await self._all(f"SELECT {_CONNECTION_COLUMNS} FROM email_connections "
                               "WHERE seller_id = %s AND status <> 'disconnected' ORDER BY provider, account_email",
                               (seller_id,))

    async def get_connection(self, seller_id: str, connection_id: str) -> dict[str, Any] | None:
        return await self._one(f"SELECT {_CONNECTION_COLUMNS} FROM email_connections "
                               "WHERE seller_id = %s AND id = %s", (seller_id, connection_id))

    async def save_connection(self, seller_id: str, provider: str, account_email: str, display_name: str | None,
                              scopes: list[str], encrypt) -> dict[str, Any]:
        """Upsert the mailbox and its credential in one transaction.

        ``encrypt(connection_id) -> (blob, key_version)`` encrypts the refresh token with the row's ID as AAD.
        """
        async with self.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute(
                    f"""INSERT INTO email_connections (seller_id, provider, account_email, display_name, scopes)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (seller_id, provider, account_email) DO UPDATE
                          SET display_name = EXCLUDED.display_name, scopes = EXCLUDED.scopes, status = 'connected',
                              connected_at = now(), disconnected_at = NULL, last_error_code = NULL, updated_at = now()
                        RETURNING {_CONNECTION_COLUMNS}""",
                    (seller_id, provider, account_email, display_name, scopes))).fetchone()
                blob, version = encrypt(str(row["id"]))
                await conn.execute(
                    """INSERT INTO email_credentials (connection_id, encrypted_refresh_token, key_version)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (connection_id) DO UPDATE
                         SET encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                             key_version = EXCLUDED.key_version, updated_at = now()""",
                    (row["id"], blob, version))
                return row

    async def get_credential(self, connection_id: str) -> dict[str, Any] | None:
        return await self._one("SELECT encrypted_refresh_token, key_version FROM email_credentials "
                               "WHERE connection_id = %s AND encrypted_refresh_token IS NOT NULL", (connection_id,))

    async def update_credential(self, connection_id: str, blob: bytes, version: str) -> None:
        await self._one("UPDATE email_credentials SET encrypted_refresh_token = %s, key_version = %s, "
                        "updated_at = now() WHERE connection_id = %s RETURNING connection_id",
                        (blob, version, connection_id))

    async def set_connection_status(self, connection_id: str, status: str, error_code: str | None = None) -> None:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE email_connections SET status = %s, last_error_code = %s, updated_at = now(),
                              disconnected_at = CASE WHEN %s = 'disconnected' THEN now() ELSE disconnected_at END
                       WHERE id = %s""", (status, error_code, status, connection_id))
                if status == "disconnected":
                    await conn.execute("UPDATE email_credentials SET encrypted_refresh_token = NULL, "
                                       "updated_at = now() WHERE connection_id = %s", (connection_id,))

    async def touch_connection(self, connection_id: str) -> None:
        await self._one("UPDATE email_connections SET last_used_at = now() WHERE id = %s RETURNING id",
                        (connection_id,))

    # ---- OAuth transactions -----------------------------------------------------------------------

    async def create_transaction(self, seller_id: str, provider: str, state_hash: str, ttl_seconds: int,
                                 verifier_blob: bytes, verifier_version: str, transaction_id: str) -> dict[str, Any]:
        return await self._one(
            """INSERT INTO email_oauth_transactions
                 (id, seller_id, provider, state_hash, encrypted_verifier, verifier_key_version, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
               RETURNING id, provider, expires_at""",
            (transaction_id, seller_id, provider, state_hash, verifier_blob, verifier_version, ttl_seconds))

    async def authorize_transaction(self, state_hash: str, code_blob: bytes | None, code_version: str | None,
                                    denied: bool) -> dict[str, Any] | None:
        """Browser callback: record the code (or the denial) on the matching pending, unexpired attempt."""
        return await self._one(
            """UPDATE email_oauth_transactions
                  SET status = %s, encrypted_code = %s, code_key_version = %s
                WHERE state_hash = %s AND status = 'pending' AND expires_at > now()
                RETURNING id, provider""",
            ("denied" if denied else "authorized", code_blob, code_version, state_hash))

    async def transaction_status(self, seller_id: str, transaction_id: str) -> dict[str, Any] | None:
        return await self._one(
            """SELECT id, provider, expires_at,
                      CASE WHEN status IN ('pending', 'authorized') AND expires_at <= now() THEN 'expired'
                           ELSE status END AS status
                 FROM email_oauth_transactions WHERE seller_id = %s AND id = %s""", (seller_id, transaction_id))

    async def consume_transaction(self, seller_id: str, transaction_id: str, provider: str) -> dict[str, Any] | None:
        """Single use: only the seller who started it, once, before it expires."""
        # Read the secrets and mark the row consumed (wiping the code) in one statement.
        return await self._one(
            """WITH tx AS (
                   SELECT id, state_hash, encrypted_verifier, verifier_key_version, encrypted_code, code_key_version
                     FROM email_oauth_transactions
                    WHERE id = %s AND seller_id = %s AND provider = %s AND status = 'authorized'
                      AND expires_at > now()
                      FOR UPDATE),
               done AS (
                   UPDATE email_oauth_transactions t SET status = 'consumed', encrypted_code = NULL
                     FROM tx WHERE t.id = tx.id RETURNING t.id)
               SELECT tx.* FROM tx JOIN done USING (id)""",
            (transaction_id, seller_id, provider))

    async def set_next_url(self, seller_id: str, transaction_id: str, url: str) -> bool:
        """Remember where the sign-in tab should go next (the draft), for 30 minutes after sign-in started."""
        row = await self._one("UPDATE email_oauth_transactions SET next_url = %s WHERE id = %s AND seller_id = %s "
                              "AND created_at > now() - interval '30 minutes' RETURNING id",
                              (url, transaction_id, seller_id))
        return row is not None

    async def next_url(self, state_hash: str) -> str | None:
        row = await self._one("SELECT next_url FROM email_oauth_transactions WHERE state_hash = %s "
                              "AND created_at > now() - interval '30 minutes'", (state_hash,))
        return row["next_url"] if row else None

    async def purge_transactions(self) -> None:
        await self._one("DELETE FROM email_oauth_transactions WHERE expires_at < now() - interval '1 day' "
                        "RETURNING 1")

    # ---- drafts ----------------------------------------------------------------------------------

    async def drafts_in_last_hour(self, seller_id: str) -> int:
        row = await self._one("SELECT count(*) AS n FROM email_draft_log WHERE seller_id = %s "
                              "AND created_at > now() - interval '1 hour'", (seller_id,))
        return int(row["n"]) if row else 0

    async def log_draft(self, values: dict[str, Any]) -> None:
        columns = list(values)
        await self._one(f"INSERT INTO email_draft_log ({', '.join(columns)}) "
                        f"VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id", tuple(values.values()))

    # ---- audit -----------------------------------------------------------------------------------

    async def audit(self, seller_id: str, action: str, connection_id: str | None = None,
                    details: dict[str, Any] | None = None) -> None:
        await self._one("INSERT INTO email_audit_logs (seller_id, connection_id, action, details) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (seller_id, connection_id, action, json.dumps(details or {})))
