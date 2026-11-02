from typing import Any, Literal

from pydantic import BaseModel, Field

Outcome = Literal["generated", "failed", "clarification"]
Signal = Literal["saved", "exported", "shared", "rerun", "up", "down",
                 "rating:1", "rating:2", "rating:3", "rating:4", "rating:5"]


class LookupRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000, description="The privacy-filtered question")
    scope_key: str = Field(max_length=200, description="Shape of the UI scope (source, status, date field), not dates")
    k: int = Field(default=3, ge=0, le=10)


class Example(BaseModel):
    prompt: str
    plan: dict[str, Any]
    similarity: float
    score: float


class LookupResponse(BaseModel):
    cached_plan: dict[str, Any] | None
    examples: list[Example]


class RecordRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    scope_key: str = Field(max_length=200)
    plan: dict[str, Any]
    report_id: str | None = Field(default=None, max_length=64)
    outcome: Outcome
    cacheable: bool = False


class FeedbackRequest(BaseModel):
    report_id: str = Field(max_length=64)
    signal: Signal
