from app.core.config import get_settings
from app.core.module_base import BaseModule

from .router import router
from .service import Service


class Module(BaseModule):
    name = 'attachments'
    title = 'Attachments'
    description = 'Internal file extraction, source planning context and template edits.'

    def get_router(self):
        return router

    async def on_startup(self, app):
        app.state.attachments_service = Service(get_settings())
