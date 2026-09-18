"""The feed itself: recent flags, newest first, and one event in full."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from ..store.base import FeedFilter
from .deps import get_store
from .schemas import EventOut

router = APIRouter()


@router.get("/feed", response_model=list[EventOut])
async def feed(
    min_score: int = Query(0, ge=0, le=100),
    since: datetime | None = None,
    ticker: str | None = None,
    source: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[EventOut]:
    """Flags newest first. Every filter is optional and they compose."""
    rows = await get_store().feed(
        FeedFilter(min_score=min_score, since=since, ticker=ticker, source=source, limit=limit)
    )
    return [EventOut.of(r) for r in rows]


@router.get("/event/{event_id}", response_model=EventOut)
async def event(event_id: int) -> EventOut:
    """One event with its raw payload, for the click-through from a feed row."""
    row = await get_store().get_event(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such event")
    return EventOut.of(row, include_payload=True)
