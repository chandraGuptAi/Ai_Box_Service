from datetime import date
from typing import Any

from pydantic import BaseModel


class CallFilters(BaseModel):
    """Query filters, passed to Voice AI as they are (all optional)."""
    client_id: str | None = None
    type: str = "all"
    campaign_id: str | None = None
    status: str | None = None
    outcome: str | None = None
    phone: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    limit: int = 50
    offset: int = 0


class CallLogSummary(BaseModel):
    calls: int = 0
    answered: int = 0
    not_answered: int = 0
    talk_time_s: int = 0
    cost_rupees: float = 0


class CallLogsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    summary: CallLogSummary | None = None
    #: Calls as Voice AI returns them; recording links point to this service's recording endpoint.
    calls: list[dict[str, Any]]
