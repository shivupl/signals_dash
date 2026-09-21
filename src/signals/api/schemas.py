"""Response models. The wire format the UI depends on."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from ..store.base import EventRow, SourceLatency, WatchlistRow


class EventOut(BaseModel):
    id: int
    ticker: str | None
    company: str | None
    source: str
    event_type: str
    #: Normalized across sources; see signals/categories.py.
    category: str | None = None
    occurred_at: datetime
    ingested_at: datetime
    summary: str | None
    #: What happened, in plain language -- the line you scan.
    headline: str
    #: Where it came from: form, item, source. The line underneath.
    detail: str
    score: int
    tier: str
    url: str | None
    price_at: float | None
    #: Percent move since the flag fired; null until both prices are known.
    change_since: float | None = None
    #: Itemized reasons, so the UI can answer "why is this an 85?"
    score_parts: dict[str, int]
    payload: dict[str, Any] | None = None

    @classmethod
    def of(cls, row: EventRow, *, include_payload: bool = False) -> EventOut:
        return cls(
            id=row.id,
            ticker=row.ticker,
            company=row.company_name or row.payload.get("filer_name"),
            source=row.source,
            event_type=row.event_type,
            category=row.category,
            occurred_at=row.occurred_at,
            ingested_at=row.ingested_at,
            summary=row.summary,
            headline=row.payload.get("headline") or row.summary or row.event_type,
            detail=row.payload.get("detail") or row.source.replace("_", " "),
            score=row.score,
            tier=tier_for(row.score),
            url=row.url,
            price_at=float(row.price_at) if row.price_at is not None else None,
            change_since=row.change_since,
            score_parts=row.payload.get("score_parts") or {},
            payload=row.payload if include_payload else None,
        )


def tier_for(score: int) -> str:
    """The three bands the feed colours by."""
    if score >= 85:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 30:
        return "background"
    return "quiet"


class WatchlistOut(BaseModel):
    company_id: int
    ticker: str | None
    name: str
    flags: int
    top_score: int
    last_event_at: datetime | None
    last_price: float | None = None
    week_change: float | None = None
    next_earnings: date | None = None

    @classmethod
    def of(cls, row: WatchlistRow) -> WatchlistOut:
        return cls(
            company_id=row.company_id,
            ticker=row.ticker,
            name=row.name,
            flags=row.flags,
            top_score=row.top_score,
            last_event_at=row.last_event_at,
            last_price=float(row.last_price) if row.last_price is not None else None,
            week_change=row.week_change,
            next_earnings=row.next_earnings,
        )


class SourceLatencyOut(BaseModel):
    source: str
    events: int
    p50_seconds: float | None
    p95_seconds: float | None

    @classmethod
    def of(cls, row: SourceLatency) -> SourceLatencyOut:
        return cls(source=row.source, events=row.events, p50_seconds=row.p50, p95_seconds=row.p95)


class SystemEventOut(BaseModel):
    """One line of the status strip."""

    id: int
    adapter: str | None
    state: str  # "open" | "resolved" | "info"
    event_type: str
    headline: str
    detail: str | None
    occurred_at: datetime
    minutes: float | None

    @classmethod
    def of(cls, row: EventRow) -> SystemEventOut:
        p = row.payload
        return cls(
            id=row.id,
            adapter=p.get("adapter"),
            state=p.get("state") or "info",
            event_type=row.event_type,
            headline=p.get("headline") or row.summary or row.event_type,
            detail=p.get("detail"),
            occurred_at=row.occurred_at,
            minutes=p.get("minutes"),
        )


class SettingsOut(BaseModel):
    flag_threshold: int
    #: False when no ADMIN_TOKEN is configured: the panel is then read-only.
    editable: bool


class SettingsIn(BaseModel):
    flag_threshold: int


class StatsOut(BaseModel):
    latency: list[SourceLatencyOut]
    unresolved: int
    flag_threshold: int
    #: Events the latency figures ignore on purpose: the opening backfill of a
    #: poll window, and anything loaded by replaying fixtures. Reported so the
    #: number is visibly a subset rather than silently one.
    excluded_from_latency: int = 0
    latency_note: str = (
        "Latency covers only filings accepted after the worker started. "
        "Backfilled and replayed events are excluded."
    )
