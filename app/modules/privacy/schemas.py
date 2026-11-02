from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class FilterRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    identities: list[str] = Field(default_factory=list, max_length=100_000,
                                  description="Known personal values of this request (seller name, IDs); masked exactly.")
    field_names: list[str] = Field(default_factory=list, max_length=1000,
                                   description="Report metric/field names that may appear as `name` in operation metadata.")


class Removed(BaseModel):
    category: str
    ref: str = Field(description="Fingerprint of the removed value, for counting it once across calls. Not the value.")


class FilterResponse(BaseModel):
    messages: list[Message]
    removed: list[Removed]
