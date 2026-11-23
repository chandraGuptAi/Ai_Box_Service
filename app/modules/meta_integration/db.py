"""PostgreSQL access and the module's SQL migration runner.

The database is the one AI_python_backend uses (``DATABASE_URL``); every table here is prefixed
``meta_`` and owned by this module's migrations.

psycopg's async mode cannot run on Windows' default ProactorEventLoop (used by uvicorn on
Windows), so this module uses psycopg's synchronous pool and runs each database call in a worker
thread through a small async adapter. The repository code is written against the adapter's
``await conn.execute(...)`` / ``async with conn.transaction()`` interface.

Migrations are the ``migrations/NNNN_*.sql`` files, applied in order inside one transaction each,
under an advisory lock so concurrent processes (API + worker) never apply the same file twice.
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

logger = get_logger("module.meta_integration.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x4D455441  # "META"


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

    async def executemany(self, query: str, params_seq) -> None:
        await asyncio.to_thread(self._cur.executemany, query, list(params_seq))


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

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[AsyncCursor]:
        cur = self._conn.cursor()
        try:
            yield AsyncCursor(cur)
        finally:
            cur.close()


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


async def open_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> AsyncPool:
    # autocommit: each statement commits on its own; multi-statement work uses conn.transaction().
    pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False, name="meta_integration",
                          kwargs={"row_factory": dict_row, "autocommit": True, "connect_timeout": 10})
    await asyncio.to_thread(pool.open, True, 15.0)
    return AsyncPool(pool)


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


async def migrate(conn: AsyncConnection) -> list[str]:
    """Apply pending migrations; returns the versions applied now."""
    applied_now: list[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        await conn.execute("""CREATE TABLE IF NOT EXISTS meta_schema_migrations (
                                  version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        rows = await (await conn.execute("SELECT version FROM meta_schema_migrations")).fetchall()
        done = {r["version"] for r in rows}
        for path in migration_files():
            if path.stem in done:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO meta_schema_migrations (version) VALUES (%s)", (path.stem,))
            applied_now.append(path.stem)
            logger.info("Applied Meta migration %s", path.stem)
    return applied_now
