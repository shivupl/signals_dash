"""The rail, the numbers, and a liveness check."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query

from .deps import get_settings, get_store
from .schemas import SourceLatencyOut, StatsOut, WatchlistOut

router = APIRouter()


@router.get("/watchlist", response_model=list[WatchlistOut])
async def watchlist(min_score: int = Query(30, ge=0, le=100)) -> list[WatchlistOut]:
    """Every watched company with its week. A heat map of where to look."""
    rows = await get_store().watchlist(min_score)
    return [WatchlistOut.of(r) for r in rows]


@router.get("/stats", response_model=StatsOut)
async def stats(hours: int = Query(24, ge=1, le=720)) -> StatsOut:
    """Detection lag per source, measured rather than asserted.

    Only filings the worker was running to see are counted. The first poll of a
    100-entry window returns filings accepted hours earlier, and replayed
    fixtures carry their original acceptance times -- counting either would make
    this report the age of the backlog rather than how fast we notice.
    """
    store = get_store()
    window = timedelta(hours=hours)
    latency = await store.latency_percentiles(window)
    return StatsOut(
        latency=[SourceLatencyOut.of(r) for r in latency],
        unresolved=await store.count_unresolved(),
        flag_threshold=get_settings().flag_threshold,
        excluded_from_latency=await store.count_excluded_from_latency(window),
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
