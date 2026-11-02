from fastapi import APIRouter, Request

from app.core.module_base import BaseModule


class Module(BaseModule):
    name = "health"
    description = "Liveness check and list of loaded modules."

    def get_router(self) -> APIRouter:
        router = APIRouter()

        @router.get("")
        async def health(request: Request):
            return {
                "status": "ok",
                "modules": [m.name for m in request.app.state.modules.all()],
            }

        return router
