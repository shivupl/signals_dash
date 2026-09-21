"""The feed: recent events, newest first, and one event in full."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Query, Response

from ..categories import CATEGORY_LABELS
from ..clock import EASTERN
from ..store.base import FeedFilter
from .deps import get_store
from .schemas import EventOut

router = APIRouter()

RANGES = ("today", "7d", "30d")


def split(value: str | None) -> tuple[str, ...]:
    """Comma-separated multi-select, which keeps filter URLs short and readable."""
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def window(
    range_: str | None, since: datetime | None, until: datetime | None, now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """Resolve a named range to instants. Explicit since/until win.

    "today" is the market's today: a filing at 19:00 ET on the 18th belongs to the
    18th wherever the viewer happens to be sitting.
    """
    if since is not None or until is not None:
        return since, until
    if not range_:
        return None, None
    now = now or datetime.now(tz=UTC)
    if range_ == "today":
        midnight = datetime.combine(now.astimezone(EASTERN).date(), time(0, 0), tzinfo=EASTERN)
        return midnight.astimezone(UTC), None
    days = {"7d": 7, "30d": 30}.get(range_)
    if days is None:
        raise HTTPException(status_code=422, detail=f"range must be one of {RANGES}")
    return now - timedelta(days=days), None


def build_filter(
    *,
    min_score: int,
    ticker: str | None,
    source: str | None,
    category: str | None,
    range_: str | None,
    since: datetime | None,
    until: datetime | None,
    system: bool,
    limit: int = 200,
    offset: int = 0,
) -> FeedFilter:
    categories = split(category)
    unknown = [c for c in categories if c not in CATEGORY_LABELS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown category: {', '.join(unknown)}")
    start, end = window(range_, since, until)
    return FeedFilter(
        min_score=min_score,
        since=start,
        until=end,
        tickers=split(ticker),
        sources=split(source),
        categories=categories,
        include_system=system,
        limit=limit,
        offset=offset,
    )


@router.get("/feed", response_model=list[EventOut])
async def feed(
    response: Response,
    min_score: int = Query(0, ge=0, le=100),
    ticker: str | None = Query(None, description="comma-separated"),
    source: str | None = Query(None, description="comma-separated"),
    category: str | None = Query(None, description="comma-separated"),
    range_: str | None = Query(None, alias="range"),
    since: datetime | None = None,
    until: datetime | None = None,
    system: bool = Query(False, description="include the pipeline's own events"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[EventOut]:
    """Events newest first. Every filter is optional and they compose.

    ``X-Total-Count`` carries the number of matches before the limit, from the same
    predicate as the rows -- it is what the header shows, so the two cannot differ.
    """
    filters = build_filter(
        min_score=min_score,
        ticker=ticker,
        source=source,
        category=category,
        range_=range_,
        since=since,
        until=until,
        system=system,
        limit=limit,
        offset=offset,
    )
    store = get_store()
    response.headers["X-Total-Count"] = str(await store.feed_count(filters))
    # A row scoring 0 is on the record, but it is not a flag. With the score
    # filter at 0 the header still has to be able to say how many flags there are.
    flags_only = replace(
        filters,
        min_score=max(filters.min_score, 1),
        include_system=False,
        sources=tuple(s for s in filters.sources if s != "system"),
    )
    response.headers["X-Flag-Count"] = str(await store.feed_count(flags_only))
    return [EventOut.of(r) for r in await store.feed(filters)]


@router.get("/event/{event_id}", response_model=EventOut)
async def event(event_id: int) -> EventOut:
    """One event with its raw payload, for the click-through from a feed row."""
    row = await get_store().get_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such event")
    return EventOut.of(row, include_payload=True)
