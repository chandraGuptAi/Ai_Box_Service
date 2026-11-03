"""Audit log: AI calling (Voice AI) call logs, analyses and recordings for admins."""
from fastapi import APIRouter

from app.core.config import get_settings
from app.core.module_base import BaseModule

from .client import VoiceAIAdminClient
from .router import router
from .service import Service


class Module(BaseModule):
    name = "audit_log"
    title = "Audit Log (AI calling)"
    description = "Voice AI call logs, analyses and recordings of every client (admin key required)."
    depends_on: list[str] = []

    def get_router(self) -> APIRouter:
        return router

    async def on_startup(self, app) -> None:
        settings = get_settings()
        base_url = settings.VOICEAI_BASE_URL or self.config.get("base_url")
        if not base_url:
            raise RuntimeError("audit_log needs VOICEAI_BASE_URL in .env or `base_url` in modules.yaml")
        if not settings.VOICEAI_ADMIN_KEY:
            self.logger.warning("VOICEAI_ADMIN_KEY is empty; Voice AI will reject every audit log request")
        self.client = VoiceAIAdminClient(base_url, settings.VOICEAI_ADMIN_KEY,
                                         float(self.config.get("timeout_seconds", 30)))
        recording = settings.API_PREFIX + self.prefix + "/call-logs/{call_id}/recording"
        app.state.audit_log_service = Service(self.client, recording)
        self.logger.info("started (Voice AI: %s)", base_url)

    async def on_shutdown(self, app) -> None:
        await self.client.close()
