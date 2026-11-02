"""Complete shipment fetch from the Bigship order-list API (moved from AI_python_backend's shipment_source.py).

- POST ``{base}/api/get-order-list`` with the seller's ``Authorization: Bearer <token>``; the token decides
  which seller's shipments are returned.
- Rows: ``data.getOrderList``; matching total: ``data.totalRecords``.

The matching total is read first; pages are sized from it and read concurrently. Large totals are read as
date windows (each counted, then read). A page the server cannot serve (5xx or timeout) is split in half
and read again. A clamped page size is detected and read again; repeated, empty or overlapping pages and
a changing total stop the fetch. Nothing is ever truncated: the result has exactly ``total`` rows.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any

import httpx

from app.core.logger import get_logger

from .errors import (RETRY_CODES, SPLIT_CODES, FetchError, changed, incomplete, pagination, timed_out,
                     too_large, unavailable)

logger = get_logger("module.shipments.fetch")

ACCOUNT_COUNTER_KEYS = ("all_orders", "pickup_scheduled", "not_pickup", "in_transit", "out_for_delivery", "delivered",
                        "rto_in_transit", "rto_delivered", "undelivered", "cancel", "lost")
_STAMP = "%Y-%m-%d %H:%M:%S"
_MIN_SPLIT_ROWS = 25      # a failing page is not split below this many rows
_PAGE_STEP = 16           # page sizes are multiples of this, so a failing page halves exactly
_ORDER_KEYS = ("orderid", "mastercustomorderid", "customorderid", "id")
_AWB_KEYS = ("awbnumber", "awbno", "referencenumber", "lrnumber", "trackingnumber")


@dataclass(frozen=True)
class Limits:
    path: str = "/api/get-order-list"
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    total_timeout: float = 60.0
    retries: int = 1
    fetch_attempts: int = 2
    max_records: int = 5000
    concurrency: int = 4
    window_records: int = 500
    min_page_size: int = 100
    max_page_size: int = 5000
    max_requests: int = 30
    max_response_bytes: int = 64 * 1024 * 1024
    count_page_size: int = 1


@dataclass(frozen=True)
class Query:
    """The one upstream filter set; every request of a fetch sends exactly these values."""
    status_type: int
    from_date: str                    # IST "YYYY-MM-DD HH:MM:SS", inclusive
    to_date: str                      # exclusive
    date_filter_type: str
    segment_type: str

    def body(self, page: int, per_page: int) -> dict[str, Any]:
        return {"type": self.status_type, "page": page, "perPage": per_page, "search": "",
                "search_by_courier": "", "search_by_order_id": "", "search_awb_number": "",
                "search_lrn_number": "", "search_by_cust_id": "", "search_by_company_name": "",
                "search_by_seller_email": "", "only_order_stats": 0, "segment_type": self.segment_type,
                "fromDate": self.from_date, "toDate": self.to_date, "date_filter_type": self.date_filter_type}


@dataclass
class Page:
    number: int
    per_page: int
    rows: list[dict[str, Any]]
    total: int
    summary: dict[str, int]


@dataclass
class FetchResult:
    rows: list[dict[str, Any]]
    total: int
    account_summary: dict[str, int]
    fetched_at: str
    requests: int
    page_size: int
    pages: int
    attempts: int
    page_size_clamped: bool
    extra: dict[str, Any] = field(default_factory=dict)


class _Clamped(Exception):
    def __init__(self, rows: int) -> None:
        super().__init__(rows)
        self.rows = rows


class _WindowMismatch(Exception):
    def __init__(self, found: int) -> None:
        super().__init__(found)
        self.found = found


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _identity(record: dict[str, Any]) -> tuple[str, str] | None:
    flat = {_norm(k): v for k, v in record.items() if not isinstance(v, (dict, list))}

    def first(keys):
        value = next((flat[k] for k in keys if flat.get(k) not in (None, "")), None)
        return re.sub(r"[^A-Za-z0-9_\-/]", "", str(value))[:60] if value is not None else ""
    order_id, awb = first(_ORDER_KEYS), first(_AWB_KEYS)
    return (order_id, awb) if order_id or awb else None


def _split_error(exc: BaseException) -> bool:
    return isinstance(exc, FetchError) and exc.code in SPLIT_CODES


async def _all(coroutines):
    """gather() that cancels the remaining requests as soon as one fails."""
    tasks = [asyncio.ensure_future(c) for c in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class ShipmentFetcher:
    def __init__(self, base_url: str, limits: Limits, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._limits = limits
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, follow_redirects=False,
                                         timeout=httpx.Timeout(limits.read_timeout, connect=limits.connect_timeout))

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, query: Query, credential: str, max_records: int | None = None) -> FetchResult:
        cfg = self._limits
        deadline = time.monotonic() + cfg.total_timeout
        started, last = time.monotonic(), None
        for attempt in range(1, cfg.fetch_attempts + 1):
            try:
                result = await self._fetch_once(query, credential, deadline, max_records or cfg.max_records)
            except FetchError as exc:
                if exc.code not in RETRY_CODES:
                    raise
                last = exc
                logger.warning("Shipment fetch attempt %s inconsistent (%s); %s", attempt, exc.code,
                               "retrying" if attempt < cfg.fetch_attempts else "giving up")
                continue
            result.attempts = attempt
            logger.info("Shipment fetch complete: total=%s requests=%s page_size=%s clamped=%s attempts=%s ms=%d",
                        result.total, result.requests, result.page_size, result.page_size_clamped, attempt,
                        (time.monotonic() - started) * 1000)
            return result
        raise last

    async def _fetch_once(self, query: Query, credential: str, deadline: float, max_records: int) -> FetchResult:
        cfg = self._limits
        calls, failure = 0, None
        semaphore = asyncio.Semaphore(max(1, cfg.concurrency))

        async def page(q: Query, number: int, per_page: int, retries: int | None = None) -> Page:
            nonlocal calls, failure
            if calls >= cfg.max_requests:
                if failure is not None:
                    raise failure  # recovering from upstream failures used the request budget: report those
                raise too_large("Too many shipments match this range. Select a shorter date range.",
                                {"max_requests": cfg.max_requests})
            calls += 1
            async with semaphore:
                try:
                    return await self._call(q, number, per_page, credential, deadline, retries)
                except FetchError as exc:
                    if exc.code in SPLIT_CODES:
                        failure = exc
                    raise

        count = await page(query, 1, cfg.count_page_size)
        total = count.total
        if total == 0:
            raise FetchError("no_matching_records", "No shipments matched the selected dates and status.", 422,
                             details={"expected_records": 0})
        limit = min(cfg.max_records, max_records)
        if total > limit:
            raise too_large(f"Too many shipments match this range ({total:,}; a report supports at most {limit:,}). "
                            "Select a shorter date range or a single status.",
                            {"expected_records": total, "max_records": limit})
        try:
            rows, pages, size, clamped = await self._read_range(page, query, total, deadline, windows=True, top=True)
        except _WindowMismatch as mismatch:
            logger.warning("Shipment date windows matched %s of %s shipments; reading the whole range by pages.",
                           mismatch.found, total)
            rows, pages, size, clamped = await self._read_range(page, query, total, deadline, windows=False, top=True)
        if len(rows) != total:
            raise incomplete(f"Received {len(rows):,} of {total:,} matching shipments. Please retry.",
                             {"expected_records": total, "fetched_records": len(rows)})
        return FetchResult(rows=rows, total=total, account_summary=count.summary,
                           fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), requests=calls,
                           page_size=size, pages=pages, attempts=1, page_size_clamped=clamped)

    async def _read_range(self, page, q: Query, total: int, deadline: float, *, windows: bool, top: bool):
        cfg = self._limits
        cuts = self._day_cuts(q) if windows else []
        if cuts and total > cfg.window_records:
            return await self._read_windows(page, q, total, deadline, cuts, math.ceil(total / cfg.window_records))
        try:
            return await self._read_paged(page, q, total, deadline, single=not top, split_pages=not cuts)
        except FetchError as exc:
            if not _split_error(exc) or not cuts or total <= cfg.min_page_size or time.monotonic() >= deadline:
                raise
            logger.warning("Shipment range %s..%s (%s shipments) failed (%s); reading it as two date windows.",
                           q.from_date, q.to_date, total, exc.code)
            return await self._read_windows(page, q, total, deadline, cuts, 2)

    async def _read_windows(self, page, q: Query, total: int, deadline: float, cuts: list[datetime], parts: int):
        parts = min(parts, len(cuts) + 1)
        days = len(cuts) + 1
        chosen = sorted({cuts[min(len(cuts), max(1, round(i * days / parts))) - 1] for i in range(1, parts)})
        bounds = [q.from_date, *(c.strftime(_STAMP) for c in chosen), q.to_date]
        subqueries = [replace(q, from_date=a, to_date=b) for a, b in zip(bounds, bounds[1:])]
        counts = await _all(page(w, 1, self._limits.count_page_size) for w in subqueries)
        found = sum(c.total for c in counts)
        logger.info("Shipment range %s..%s read as %s date windows: %s", q.from_date, q.to_date, len(subqueries),
                    [c.total for c in counts])
        if found != total:
            raise _WindowMismatch(found)
        results = await _all(self._read_range(page, w, c.total, deadline, windows=True, top=False)
                             for w, c in zip(subqueries, counts) if c.total)
        return ([row for r in results for row in r[0]], sum(r[1] for r in results),
                max(r[2] for r in results), any(r[3] for r in results))

    @staticmethod
    def _day_cuts(q: Query) -> list[datetime]:
        start, end = datetime.strptime(q.from_date, _STAMP), datetime.strptime(q.to_date, _STAMP)
        day, cuts = datetime.combine(start.date() + timedelta(days=1), dtime(0)), []
        while day < end:
            cuts.append(day)
            day += timedelta(days=1)
        return cuts

    async def _read_paged(self, page, q: Query, total: int, deadline: float, *, single: bool, split_pages: bool):
        def same_total(p: Page) -> None:
            if p.total != total:
                raise changed("The shipments changed while they were being loaded. Please try again.",
                              {"expected_records": total, "latest_total": p.total})

        size, clamped = (min(total, self._limits.max_page_size) if single else self._page_size(total)), False
        while True:
            try:
                chunks = await self._read_pages(page, q, total, size, deadline, split_pages)
                return self._assemble(chunks, total, same_total), len(chunks), size, clamped
            except _Clamped as clamp:
                if clamped:
                    raise pagination("The shipment service returned pages of changing size. Please try again.",
                                     {"expected_rows": size, "received_rows": clamp.rows})
                size, clamped = clamp.rows, True

    def _page_size(self, total: int) -> int:
        cfg = self._limits
        size = max(math.ceil(total / max(1, cfg.concurrency)), cfg.min_page_size)
        size = math.ceil(size / _PAGE_STEP) * _PAGE_STEP
        return max(1, min(size, total, cfg.max_page_size))

    async def _read_pages(self, page, q: Query, total: int, size: int, deadline: float,
                          split_pages: bool) -> list[tuple[int, int, Page]]:
        async def read(number: int, per_page: int) -> list[tuple[int, int, Page]]:
            start = (number - 1) * per_page
            if start >= total:
                return []
            splittable = split_pages and per_page % 2 == 0 and per_page // 2 >= _MIN_SPLIT_ROWS
            try:
                current = await page(q, number, per_page, 0 if splittable or not split_pages else None)
            except FetchError as exc:
                if not _split_error(exc) or not splittable or time.monotonic() >= deadline:
                    raise
                half = per_page // 2
                logger.warning("Shipment API page=%s per_page=%s failed (%s); reading it as pages of %s.",
                               number, per_page, exc.code, half)
                first, second = await _all([read(2 * number - 1, half), read(2 * number, half)])
                return first + second
            return [(start, per_page, current)]

        results = await _all(read(n, size) for n in range(1, math.ceil(total / size) + 1))
        return sorted((chunk for result in results for chunk in result), key=lambda chunk: chunk[0])

    @staticmethod
    def _assemble(chunks: list[tuple[int, int, Page]], total: int, same_total) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set = set()
        previous = None
        for start, size, current in chunks:
            same_total(current)
            expected = min(size, total - start)
            details = {"page": current.number, "page_size": size, "expected_rows": expected,
                       "received_rows": len(current.rows)}
            if 0 < len(current.rows) < expected and start == 0 and not rows:
                raise _Clamped(len(current.rows))
            if not current.rows:
                raise pagination("The shipment service returned an empty page before all shipments were loaded. "
                                 "Please try again.", details)
            if previous is not None and current.rows == previous.rows:
                raise pagination("The shipment service returned the same page twice. Please try again.", details)
            keys = [k for k in (_identity(r) for r in current.rows) if k is not None]
            overlap = sum(1 for k in keys if k in seen)
            if keys and overlap == len(keys):
                raise pagination("The shipment service returned a page of shipments already loaded. Please try again.",
                                 {**details, "repeated_rows": overlap})
            if overlap:
                raise changed("The shipments changed while they were being loaded. Please try again.",
                              {**details, "repeated_rows": overlap})
            if len(current.rows) != expected:
                raise pagination("The shipment service returned a page of unexpected size. Please try again.", details)
            seen.update(keys)
            rows.extend(current.rows)
            previous = current
        if len(rows) != total:
            raise incomplete(f"Received {len(rows):,} of {total:,} matching shipments. Please retry.",
                             {"expected_records": total, "fetched_records": len(rows)})
        return rows

    async def _call(self, query: Query, number: int, per_page: int, credential: str, deadline: float,
                    retries: int | None = None) -> Page:
        cfg = self._limits
        retries = cfg.retries if retries is None else retries
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            started = time.monotonic()
            try:
                async with asyncio.timeout(remaining):
                    async with self._client.stream("POST", cfg.path, json=query.body(number, per_page),
                                                   headers={"Authorization": credential,
                                                            "Accept": "application/json"}) as resp:
                        status, headers = resp.status_code, resp.headers
                        if status >= 500 and attempt < retries:
                            logger.warning("Shipment API page=%s returned %s; retrying.", number, status)
                            await asyncio.sleep(min(0.5 * (attempt + 1), max(deadline - time.monotonic(), 0)))
                            continue
                        raw = await self._read_limited(resp) if 200 <= status < 300 else b""
            except (httpx.TimeoutException, TimeoutError):
                if attempt < retries and deadline - time.monotonic() > 0:
                    logger.warning("Shipment API page=%s timed out; retrying.", number)
                    continue
                break
            except httpx.TransportError as exc:
                logger.warning("Shipment API page=%s network error (%s).", number, type(exc).__name__)
                if attempt < retries and deadline - time.monotonic() > 0:
                    await asyncio.sleep(min(0.5 * (attempt + 1), max(deadline - time.monotonic(), 0)))
                    continue
                raise unavailable("The shipment service could not be reached. Please try again.") from None
            parsed = self._parse(status, headers, raw, number, per_page)
            logger.info("Shipment API page=%s per_page=%s rows=%s total=%s ms=%d", number, per_page, len(parsed.rows),
                        parsed.total, (time.monotonic() - started) * 1000)
            return parsed
        raise timed_out("The shipment service took too long to respond. Please try again.",
                        {"timeout_seconds": cfg.total_timeout})

    async def _read_limited(self, resp: httpx.Response) -> bytes:
        limit = self._limits.max_response_bytes
        declared = resp.headers.get("content-length", "")
        error = too_large("Too many shipments match this range. Select a shorter date range.",
                          {"max_response_bytes": limit})
        if declared.isdigit() and int(declared) > limit:
            raise error
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise error
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse(status: int, headers: httpx.Headers, raw: bytes, number: int, per_page: int) -> Page:
        details = {"upstream_status": status}
        auth = FetchError("upstream_auth_failed", "Your shipment service session has expired or is not valid. "
                          "Log in again and retry.", 401, details=details)
        forbidden = FetchError("upstream_forbidden", "Your account is not allowed to read these shipments.", 403,
                               details=details)
        if status == 401:
            raise auth
        if status == 403:
            raise forbidden
        if status == 429:
            retry_after = headers.get("retry-after", "")
            if retry_after.isdigit() and int(retry_after) <= 3600:
                details["retry_after_seconds"] = int(retry_after)
            raise FetchError("upstream_rate_limited", "The shipment service is receiving too many requests. "
                             "Please wait and try again.", 429, details=details, retryable=True)
        if status >= 500:
            raise unavailable("The shipment service is unavailable. Please try again.", details)
        if not 200 <= status < 300:
            raise FetchError("upstream_rejected", "The shipment service rejected the request.", 502, details=details)
        invalid = FetchError("upstream_invalid_response", "The shipment service returned an unreadable response.", 502,
                             details=details)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise invalid from None
        if not isinstance(body, dict):
            raise invalid
        inner = body.get("status_code")
        if body.get("success") is not True or (inner is not None and inner != 200):
            code = inner if isinstance(inner, int) else None
            if code == 401:
                raise auth
            if code == 403:
                raise forbidden
            raise FetchError("upstream_rejected", "The shipment service could not return the shipments.", 502,
                             details={**details, "upstream_status_code": code})
        data = body.get("data")
        rows = data.get("getOrderList") if isinstance(data, dict) else None
        total = data.get("totalRecords") if isinstance(data, dict) else None
        if isinstance(total, str) and total.strip().isdigit():
            total = int(total.strip())
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows) or isinstance(total, bool) \
                or not isinstance(total, int) or total < 0 or len(rows) > per_page:
            raise invalid
        summary = {k: data[k] for k in ACCOUNT_COUNTER_KEYS
                   if isinstance(data.get(k), int) and not isinstance(data.get(k), bool) and data[k] >= 0}
        return Page(number=number, per_page=per_page, rows=rows, total=total, summary=summary)
