from typing import Any

from pydantic import BaseModel, Field


class FetchRequest(BaseModel):
    status_type: int = Field(ge=0, le=99, description="/api/get-order-list `type` (0 = all statuses)")
    from_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", description="IST start, inclusive")
    to_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", description="IST end, exclusive")
    date_filter_type: str = Field(default="manifest_datetime", max_length=60)
    segment_type: str = Field(default="domestic", max_length=40)
    max_records: int | None = Field(default=None, ge=1, description="Caller's own record limit, if lower")
    fresh: bool = Field(default=False, description="Skip the short-lived cache")


class RemovedField(BaseModel):
    field: str
    reason: str
    count: int


class FetchResponse(BaseModel):
    rows: list[dict[str, Any]]
    total: int
    account_summary: dict[str, int]
    fetched_at: str
    requests: int
    page_size: int
    pages: int
    attempts: int
    page_size_clamped: bool
    cached: bool
    removed_fields: list[RemovedField]
