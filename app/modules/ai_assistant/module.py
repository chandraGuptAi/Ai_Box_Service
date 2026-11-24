"""AI assistant: the "AI Assistant" tab of the AI Box report page.

Sellers ask for small everyday tasks (an email, a WhatsApp message, what a medicine is for) and get the finished
result, like a chat assistant, without leaving the panel. The seller's own details ([shop name], [your phone]...)
are filled in from their BigShip profile after the AI answers. Public URL: /api/assistant/* through the gateway.
"""
from pathlib import Path

from fastapi import APIRouter

from app.core.module_base import BaseModule
from app.core.pg import migrate, open_pool
from app.core.seller_identity import SellerIdentity

from .config import AssistantSettings
from .engine import Assistant
from .router import router
from .store import PgChatStore

# Tests replace these with an httpx.MockTransport.
AI_TRANSPORT = None
BIGSHIP_TRANSPORT = None
# Tests set a MemoryChatStore here instead of a database.
CHAT_STORE = None

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 0x41535354  # "ASST"


class Module(BaseModule):
    name = "ai_assistant"
    title = "AI Assistant"
    description = "Writes emails and messages, explains things like medicines: small tasks done directly."
    depends_on: list[str] = ["privacy"]

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        app.state.ai_assistant = None
        app.state.ai_assistant_identity = None
        app.state.ai_assistant_chats = None
        settings = AssistantSettings()
        problems = settings.problems()
        if problems:
            self.logger.warning("AI assistant disabled: %s", "; ".join(problems))
            return
        app.state.ai_assistant = Assistant(settings, transport=AI_TRANSPORT)
        if settings.BIGSHIP_API_BASE_URL.strip():
            app.state.ai_assistant_identity = SellerIdentity(
                "bigship", settings.BIGSHIP_API_BASE_URL.strip(), cache_seconds=settings.PROFILE_CACHE_SECONDS,
                timeout_seconds=settings.PROFILE_TIMEOUT_SECONDS, transport=BIGSHIP_TRANSPORT)
        else:
            self.logger.warning("BIGSHIP_API_BASE_URL is empty: answers keep [shop name] and similar placeholders")
        app.state.ai_assistant_chats = await self._chat_store(settings)
        self.logger.info("started (model %s, chat history %s)", settings.AI_MODEL,
                         "on" if app.state.ai_assistant_chats else "off")

    async def _chat_store(self, settings: AssistantSettings):
        """Saved chats + token limits; None (chat off, /ask without limits) without a database."""
        if CHAT_STORE is not None:
            return CHAT_STORE
        if not settings.DATABASE_URL.strip():
            self.logger.warning("DATABASE_URL is empty: chat history, token usage and limits are off")
            return None
        try:
            pool = await open_pool(settings.DATABASE_URL, "ai_assistant", settings.DB_POOL_MIN, settings.DB_POOL_MAX)
        except Exception as exc:   # noqa: BLE001 - a DB outage must not stop the other modules
            self.logger.error("Chat history off: database unavailable (%s)", type(exc).__name__)
            return None
        try:
            async with pool.connection() as conn:
                await migrate(conn, MIGRATIONS_DIR, "assistant_schema_migrations", _LOCK_KEY)
        except Exception as exc:   # noqa: BLE001
            await pool.close()
            self.logger.error("Chat history off: migration failed (%s)", type(exc).__name__)
            return None
        return PgChatStore(pool)

    async def on_shutdown(self, app) -> None:
        for name in ("ai_assistant", "ai_assistant_identity", "ai_assistant_chats"):
            client = getattr(app.state, name, None)
            if client is not None:
                await client.close()
                setattr(app.state, name, None)
