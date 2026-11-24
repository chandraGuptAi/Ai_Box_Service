"""Saved conversations, token usage and per-user limits (assistant_* tables).

``PgChatStore`` is used in production; ``MemoryChatStore`` keeps the same behaviour in memory for tests.
Every user-facing query filters on ``user_id``, so one user can never read or change another user's chats.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from app.core.pg import AsyncPool

_SESSION = "id, user_id, title, created_at, updated_at"
_MESSAGE = "id, session_id, role, content, parts, files, file_context, created_at"


class ChatStore(Protocol):
    async def create_session(self, user_id: str, title: str) -> dict[str, Any]: ...
    async def list_sessions(self, user_id: str, limit: int) -> list[dict[str, Any]]: ...
    async def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None: ...
    async def rename_session(self, user_id: str, session_id: str, title: str) -> dict[str, Any] | None: ...
    async def delete_session(self, user_id: str, session_id: str) -> bool: ...
    async def add_message(self, session_id: str, role: str, content: str, parts: list | None = None,
                          files: list | None = None, file_context: str = "") -> dict[str, Any]: ...
    async def messages(self, session_id: str) -> list[dict[str, Any]]: ...
    async def record_usage(self, user_id: str, session_id: str | None, message_id: int | None, endpoint: str,
                           model: str, prompt_tokens: int, completion_tokens: int, estimated: bool) -> None: ...
    async def tokens_since(self, user_id: str, since: datetime) -> int: ...
    async def user_limits(self, user_id: str) -> dict[str, Any] | None: ...
    async def close(self) -> None: ...


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


class PgChatStore:
    def __init__(self, pool: AsyncPool) -> None:
        self.pool = pool

    async def _one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchone()

    async def _all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()

    async def create_session(self, user_id: str, title: str) -> dict[str, Any]:
        return await self._one(f"INSERT INTO assistant_sessions (user_id, title) VALUES (%s, %s) RETURNING {_SESSION}",
                               (user_id, title))

    async def list_sessions(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        return await self._all(f"SELECT {_SESSION} FROM assistant_sessions WHERE user_id = %s "
                               "ORDER BY updated_at DESC LIMIT %s", (user_id, limit))

    async def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        sid = _uuid(session_id)
        return sid and await self._one(f"SELECT {_SESSION} FROM assistant_sessions WHERE user_id = %s AND id = %s",
                                       (user_id, sid))

    async def rename_session(self, user_id: str, session_id: str, title: str) -> dict[str, Any] | None:
        sid = _uuid(session_id)
        return sid and await self._one(
            f"UPDATE assistant_sessions SET title = %s WHERE user_id = %s AND id = %s RETURNING {_SESSION}",
            (title, user_id, sid))

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        sid = _uuid(session_id)
        if not sid:
            return False
        async with self.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM assistant_sessions WHERE user_id = %s AND id = %s", (user_id, sid))
            return cur.rowcount > 0

    async def add_message(self, session_id: str, role: str, content: str, parts: list | None = None,
                          files: list | None = None, file_context: str = "") -> dict[str, Any]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                row = await (await conn.execute(
                    f"INSERT INTO assistant_messages (session_id, role, content, parts, files, file_context) "
                    f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_MESSAGE}",
                    (session_id, role, content, None if parts is None else json.dumps(parts),
                     json.dumps(files or []), file_context))).fetchone()
                await conn.execute("UPDATE assistant_sessions SET updated_at = now() WHERE id = %s", (session_id,))
        return row

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self._all(f"SELECT {_MESSAGE} FROM assistant_messages WHERE session_id = %s ORDER BY id",
                               (session_id,))

    async def record_usage(self, user_id: str, session_id: str | None, message_id: int | None, endpoint: str,
                           model: str, prompt_tokens: int, completion_tokens: int, estimated: bool) -> None:
        await self._one("INSERT INTO assistant_usage (user_id, session_id, message_id, endpoint, model, prompt_tokens, "
                        "completion_tokens, total_tokens, estimated) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING id",
                        (user_id, session_id, message_id, endpoint, model, prompt_tokens, completion_tokens,
                         prompt_tokens + completion_tokens, estimated))

    async def tokens_since(self, user_id: str, since: datetime) -> int:
        row = await self._one("SELECT COALESCE(SUM(total_tokens), 0) AS used FROM assistant_usage "
                              "WHERE user_id = %s AND created_at >= %s", (user_id, since))
        return int(row["used"]) if row else 0

    async def user_limits(self, user_id: str) -> dict[str, Any] | None:
        return await self._one("SELECT daily_tokens, weekly_tokens, monthly_tokens FROM assistant_limits "
                               "WHERE user_id = %s", (user_id,))

    async def close(self) -> None:
        await self.pool.close()


class MemoryChatStore:
    """Same behaviour as PgChatStore, kept in memory (tests)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.message_rows: list[dict[str, Any]] = []
        self.usage: list[dict[str, Any]] = []
        self.limits: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def create_session(self, user_id: str, title: str) -> dict[str, Any]:
        now = self._now()
        row = {"id": str(uuid.uuid4()), "user_id": user_id, "title": title, "created_at": now, "updated_at": now}
        self.sessions[row["id"]] = row
        return dict(row)

    async def list_sessions(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        rows = [dict(s) for s in self.sessions.values() if s["user_id"] == user_id]
        return sorted(rows, key=lambda s: s["updated_at"], reverse=True)[:limit]

    async def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        row = self.sessions.get(_uuid(session_id) or "")
        return dict(row) if row and row["user_id"] == user_id else None

    async def rename_session(self, user_id: str, session_id: str, title: str) -> dict[str, Any] | None:
        if not await self.get_session(user_id, session_id):
            return None
        self.sessions[session_id]["title"] = title
        return dict(self.sessions[session_id])

    async def delete_session(self, user_id: str, session_id: str) -> bool:
        if not await self.get_session(user_id, session_id):
            return False
        del self.sessions[session_id]
        self.message_rows = [m for m in self.message_rows if m["session_id"] != session_id]
        return True

    async def add_message(self, session_id: str, role: str, content: str, parts: list | None = None,
                          files: list | None = None, file_context: str = "") -> dict[str, Any]:
        row = {"id": next(self._ids), "session_id": session_id, "role": role, "content": content, "parts": parts,
               "files": files or [], "file_context": file_context, "created_at": self._now()}
        self.message_rows.append(row)
        self.sessions[session_id]["updated_at"] = row["created_at"]
        return dict(row)

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return [dict(m) for m in self.message_rows if m["session_id"] == session_id]

    async def record_usage(self, user_id: str, session_id: str | None, message_id: int | None, endpoint: str,
                           model: str, prompt_tokens: int, completion_tokens: int, estimated: bool) -> None:
        self.usage.append({"user_id": user_id, "session_id": session_id, "message_id": message_id,
                           "endpoint": endpoint, "model": model, "prompt_tokens": prompt_tokens,
                           "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens,
                           "estimated": estimated, "created_at": self._now()})

    async def tokens_since(self, user_id: str, since: datetime) -> int:
        return sum(u["total_tokens"] for u in self.usage if u["user_id"] == user_id and u["created_at"] >= since)

    async def user_limits(self, user_id: str) -> dict[str, Any] | None:
        return self.limits.get(user_id)

    async def close(self) -> None:
        pass
