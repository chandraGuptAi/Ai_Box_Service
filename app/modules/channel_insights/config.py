"""Channel insights settings read from .env (the rest is in modules.yaml)."""
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BASE_DIR


class InsightSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    # The AI_python_backend PostgreSQL database (channel_insight_suggestions table).
    # Empty: the chips are the fixed list in service.SUGGESTIONS.
    DATABASE_URL: str = Field("", validation_alias=AliasChoices("CHANNEL_INSIGHTS_DATABASE_URL", "DATABASE_URL"))
