"""Token limits per India (IST) day, week (from Monday) and month."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from .config import AssistantSettings
from .store import ChatStore

IST = timezone(timedelta(hours=5, minutes=30), "IST")
PERIODS = ("day", "week", "month")
_LABELS = {"day": "today's", "week": "this week's", "month": "this month's"}


@dataclass
class Period:
    name: str
    used: int
    limit: int          # 0 = no limit
    starts_at: datetime
    resets_at: datetime

    @property
    def left(self) -> int | None:
        return None if not self.limit else max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.used >= self.limit


def windows(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """(start, next start) of the current IST day, week and month."""
    local = now.astimezone(IST)
    day = datetime.combine(local.date(), time.min, IST)
    week = day - timedelta(days=local.weekday())
    month = day.replace(day=1)
    next_month = (month + timedelta(days=32)).replace(day=1)
    return {"day": (day, day + timedelta(days=1)), "week": (week, week + timedelta(days=7)),
            "month": (month, next_month)}


async def usage_status(store: ChatStore, user_id: str, settings: AssistantSettings,
                       now: datetime | None = None) -> list[Period]:
    custom = await store.user_limits(user_id) or {}
    defaults = {"day": settings.DAILY_TOKEN_LIMIT, "week": settings.WEEKLY_TOKEN_LIMIT,
                "month": settings.MONTHLY_TOKEN_LIMIT}
    columns = {"day": "daily_tokens", "week": "weekly_tokens", "month": "monthly_tokens"}
    out = []
    for name, (start, end) in windows(now or datetime.now(timezone.utc)).items():
        limit = custom.get(columns[name])
        out.append(Period(name, await store.tokens_since(user_id, start),
                          defaults[name] if limit is None else int(limit), start, end))
    return out


def blocking(periods: list[Period]) -> Period | None:
    """The period whose limit is used up (the one that resets last), or None."""
    exhausted = [p for p in periods if p.exhausted]
    return max(exhausted, key=lambda p: p.resets_at) if exhausted else None


def limit_message(period: Period) -> str:
    when = period.resets_at.astimezone(IST)
    return (f"You have used {_LABELS[period.name]} AI Assistant limit ({period.limit:,} tokens). "
            f"It resets on {when:%d %b} at {when:%I:%M %p} IST.").replace(" 0", " ")
