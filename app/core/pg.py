"""Shared PostgreSQL access for modules that keep tables in the AI_python_backend database.

psycopg's async mode cannot run on Windows' default ProactorEventLoop (used by uvicorn on Windows), so this
uses psycopg's synchronous pool and runs each call in a worker thread behind a small async adapter:
``await conn.execute(...)``, ``await cur.fetchone()``, ``async with conn.transaction()``.

Each module owns its migrations: ``migrations/NNNN_*.sql`` files applied in order, recorded in the module's
own ``<prefix>_schema_migrations`` table, under a Postgres advisory lock so concurrent processes never
apply the same file twice.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.logger import get_logger

logger = get_logger("core.pg")


class AsyncCursor:
    def __init__(self, cursor: Cursor) -> None:
        self._cur = cursor

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    async def fetchone(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._cur.fetchone)

    async def fetchall(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._cur.fetchall)


class AsyncConnection:
    """One pooled connection, used by one coroutine at a time."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    async def execute(self, query: str, params: Any = None) -> AsyncCursor:
        def run() -> Cursor:
            return self._conn.cursor().execute(query, params)
        return AsyncCursor(await asyncio.to_thread(run))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        tx = self._conn.transaction()
        await asyncio.to_thread(tx.__enter__)
        try:
            yield
        except BaseException as exc:
            await asyncio.to_thread(tx.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            await asyncio.to_thread(tx.__exit__, None, None, None)


class AsyncPool:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        conn = await asyncio.to_thread(self._pool.getconn)
        try:
            yield AsyncConnection(conn)
        finally:
            await asyncio.to_thread(self._pool.putconn, conn)

    async def close(self) -> None:
        await asyncio.to_thread(self._pool.close)


async def open_pool(dsn: str, name: str, min_size: int = 1, max_size: int = 5) -> AsyncPool:
    # autocommit: each statement commits on its own; multi-statement work uses conn.transaction().
    pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False, name=name,
                          kwargs={"row_factory": dict_row, "autocommit": True, "connect_timeout": 10})
    await asyncio.to_thread(pool.open, True, 15.0)
    return AsyncPool(pool)


async def migrate(conn: AsyncConnection, migrations_dir: Path, table: str, lock_key: int) -> list[str]:
    """Apply the pending ``NNNN_*.sql`` files in ``migrations_dir``; returns the versions applied now."""
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid migrations table name")
    applied_now: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        await conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
                                   version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r["version"] for r in await (await conn.execute(f"SELECT version FROM {table}")).fetchall()}
        for path in sorted(migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            if path.stem in done:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute(f"INSERT INTO {table} (version) VALUES (%s)", (path.stem,))
            applied_now.append(path.stem)
            logger.info("Applied migration %s (%s)", path.stem, table)
    return applied_now
