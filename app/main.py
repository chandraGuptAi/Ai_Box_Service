from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.logger import get_logger, setup_logging
from app.core.module_loader import ModuleRegistry, register_modules
from app.core.security import service_token_middleware

logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    registry: ModuleRegistry = app.state.modules
    for module in registry.all():
        await module.on_startup(app)
    yield
    for module in reversed(registry.all()):
        await module.on_shutdown(app)


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    app = FastAPI(
        title=settings.APP_NAME,
        description=settings.APP_DESCRIPTION,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )
    app.middleware("http")(service_token_middleware)
    # Added last so preflight runs before authentication and auth errors get CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ALLOWED_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
    register_modules(app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    # Without a shutdown limit, open keep-alive connections (e.g. from the gateway) can stall --reload on Windows.
    uvicorn.run("app.main:app", host=s.HOST, port=s.PORT, reload=s.DEBUG, timeout_graceful_shutdown=5)
