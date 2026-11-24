"""AI assistant settings (AI_ASSISTANT_* in .env)."""
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BASE_DIR

_LOCAL = ("localhost", "127.0.0.1", "::1")


class AssistantSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_prefix="AI_ASSISTANT_", extra="ignore")

    # Any OpenAI-compatible chat completions API; DeepSeek by default, like the rest of AI Box.
    AI_BASE_URL: str = "https://api.deepseek.com"
    AI_MODEL: str = "deepseek-chat"
    AI_API_KEY: str = ""
    # The panel waits 60 s (files make answers slower), so the AI call must finish before that.
    TIMEOUT_SECONDS: float = 55
    MAX_OUTPUT_TOKENS: int = 2000
    TEMPERATURE: float = 0.6
    # The seller's own details ([shop name], [your phone]...) come from BigShip's profile API for the login.
    # Empty: answers keep those placeholders.
    BIGSHIP_API_BASE_URL: str = Field("", validation_alias=AliasChoices("AI_ASSISTANT_BIGSHIP_API_BASE_URL",
                                                                        "BIGSHIP_API_BASE_URL"))
    PROFILE_TIMEOUT_SECONDS: float = 8
    PROFILE_CACHE_SECONDS: int = 300

    # Chat history and token usage live in the AI_python_backend PostgreSQL database (assistant_* tables).
    # Empty: chat, history and limits are off (the single-shot /ask still works, without limits).
    DATABASE_URL: str = Field("", validation_alias=AliasChoices("AI_ASSISTANT_DATABASE_URL", "DATABASE_URL"))
    DB_POOL_MIN: int = 1
    DB_POOL_MAX: int = 5
    # Tokens (prompt + answer) one user may use per IST day / week (Monday) / month; 0 = no limit.
    # A row in assistant_limits overrides these for one user.
    DAILY_TOKEN_LIMIT: int = Field(50_000, ge=0)
    WEEKLY_TOKEN_LIMIT: int = Field(250_000, ge=0)
    MONTHLY_TOKEN_LIMIT: int = Field(800_000, ge=0)
    # How much of a conversation is sent back to the AI with each new message.
    HISTORY_MESSAGES: int = Field(20, ge=2)
    HISTORY_CHARS: int = Field(30_000, ge=2_000)

    def problems(self) -> list[str]:
        """Why the assistant cannot run; empty when it is ready."""
        url = urlsplit(self.AI_BASE_URL.strip())
        problems = []
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.query or url.fragment:
            problems.append("AI_ASSISTANT_AI_BASE_URL must be an http(s) API base URL")
        elif url.scheme == "http" and url.hostname not in _LOCAL:
            problems.append("AI_ASSISTANT_AI_BASE_URL must use https unless it is a local endpoint")
        if not self.AI_MODEL.strip():
            problems.append("AI_ASSISTANT_AI_MODEL is empty")
        if not self.AI_API_KEY.strip() and url.hostname not in _LOCAL:
            problems.append("AI_ASSISTANT_AI_API_KEY is empty")
        return problems
