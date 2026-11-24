from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .engine import MAX_INSTRUCTION_CHARS, Language, Task
from .files import MAX_FILES


class AskRequest(BaseModel):
    instruction: str = Field(default="", max_length=MAX_INSTRUCTION_CHARS,
                             description="What the user wants, in English, Hindi or Hinglish")
    # "Reply in" from the panel; empty: the same language the user wrote in.
    language: Language | None = None
    # The chip the user picked (Email, WhatsApp message...); empty or "anything": no hint.
    task: Task | None = None
    # Files uploaded through POST /api/attachments (Excel, Word, PDF), read for this request.
    attachment_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(
        default_factory=list, max_length=MAX_FILES)

    @field_validator("attachment_ids")
    @classmethod
    def unique_files(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Each file can be attached once.")
        return value

    @model_validator(mode="after")
    def something_to_do(self) -> "AskRequest":
        # Files alone are enough: the AI then summarises them.
        if not self.instruction.strip() and not self.attachment_ids:
            raise ValueError("Write what you need, or attach a file.")
        return self


class AnswerPart(BaseModel):
    kind: Literal["text", "copy"] = Field(description="text: intro, tips, explanation; copy: ready-to-use content")
    text: str


class AskResponse(BaseModel):
    answer: str = Field(description="The whole answer as plain text")
    parts: list[AnswerPart] = Field(description="The answer in order; show each 'copy' part with its own Copy button")


# ---- chat (saved conversations) ----

class ChatRequest(BaseModel):
    """A new message; without session_id a new conversation is started."""
    session_id: str | None = Field(None, pattern=r"^[0-9a-fA-F-]{36}$")
    content: str = Field(default="", max_length=MAX_INSTRUCTION_CHARS)
    attachment_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(
        default_factory=list, max_length=MAX_FILES)

    @field_validator("attachment_ids")
    @classmethod
    def unique_files(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Each file can be attached once.")
        return value

    @model_validator(mode="after")
    def something_to_say(self) -> "ChatRequest":
        if not self.content.strip() and not self.attachment_ids:
            raise ValueError("Write a message, or attach a file.")
        return self


class Session(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def tidy(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("The title cannot be empty.")
        return value


class AttachedFile(BaseModel):
    name: str
    format: str


class Message(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str = Field(description="User: what they wrote. Assistant: the answer as plain text")
    parts: list[AnswerPart] | None = Field(None, description="Assistant answers, in order (as in /ask)")
    files: list[AttachedFile] = Field(default_factory=list)
    created_at: datetime


class SessionDetail(BaseModel):
    session: Session
    messages: list[Message]


class PeriodUsage(BaseModel):
    period: Literal["day", "week", "month"]
    used: int = Field(description="Tokens used in this IST period")
    limit: int = Field(description="0 = no limit")
    left: int | None
    resets_at: datetime


class UsageResponse(BaseModel):
    periods: list[PeriodUsage]
    blocked: bool = Field(description="True when a limit is used up: new messages are refused")
    message: str | None = None
