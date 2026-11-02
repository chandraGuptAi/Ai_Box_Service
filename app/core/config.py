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
    # Shared secret with the AI_python_backend gateway; empty disables the check (local dev only).
    SERVICE_TOKEN: str = ""
    MODULES_FILE: Path = BASE_DIR / "modules.yaml"
    ATTACHMENT_STORAGE_DIR: Path = BASE_DIR / 'data' / 'attachments'
    ATTACHMENT_MAX_BYTES: int = 20 * 1024 * 1024
    ATTACHMENT_TTL_SECONDS: int = 86400
    ATTACHMENT_OCR_LANGUAGE: str = 'eng'
    ATTACHMENT_VISION_BASE_URL: str = ''
    ATTACHMENT_VISION_MODEL: str = ''
    ATTACHMENT_VISION_API_KEY: str = ''
    ATTACHMENT_VISION_MAX_IMAGES: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
