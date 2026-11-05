"""Global application settings, loaded from environment / .env file."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    APP_NAME: str = "AI Box API"
    APP_DESCRIPTION: str = "AI Box backend for Bigship. Send the Bigship panel token as `Authorization: Bearer <token>`."
    APP_VERSION: str = "0.1.0"
    ENV: str = "development"
    DEBUG: bool = True
    HOST: str = "0.0.0.0"
    PORT: int = 8100
    API_PREFIX: str = "/api"
    LOG_LEVEL: str = "INFO"
    # JSON array in .env; use explicit frontend origins.
    CORS_ALLOWED_ORIGINS: list[str] = ["http://localhost:4200"]
    # Shared secret with the AI_python_backend gateway; empty disables the check (local dev only).
    SERVICE_TOKEN: str = ""
    MODULES_FILE: Path = BASE_DIR / "modules.yaml"
    ATTACHMENT_STORAGE_DIR: Path = BASE_DIR / 'data' / 'attachments'
    ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024  # 0 = no upload size limit
    ATTACHMENT_TTL_SECONDS: int = 86400
    ATTACHMENT_OCR_LANGUAGE: str = 'eng'
    ATTACHMENT_VISION_BASE_URL: str = ''
    ATTACHMENT_VISION_MODEL: str = ''
    ATTACHMENT_VISION_API_KEY: str = ''
    ATTACHMENT_VISION_MAX_IMAGES: int = 10
    # Audit log module: Voice AI (AI calling) admin API. The admin key never leaves this server.
    VOICEAI_BASE_URL: str = ""
    VOICEAI_ADMIN_KEY: str = ""
    # Key callers send in X-Audit-Key to read audit logs; empty disables the audit log API.
    AUDIT_LOG_API_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
