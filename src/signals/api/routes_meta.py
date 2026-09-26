"""The rail, the numbers, and a liveness check."""

from __future__ import annotations

import hmac
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Query

from ..categories import CATEGORY_LABELS
from .deps import get_settings, get_store
from .schemas import (
    ActiveOut,
    SettingsIn,
    SettingsOut,
    SourceLatencyOut,
    StatsOut,
    SystemEventOut,
    WatchlistOut,
)

router = APIRouter()


@router.get("/watchlist", response_model=list[WatchlistOut])
async def watchlist(
    min_score: int = Query(30, ge=0, le=100),
    source: str | None = None,
    category: str | None = None,
    universe: str | None = None,
) -> list[WatchlistOut]:
    """Every watched company with its week, under the feed's own filters."""
    from .routes_feed import UNIVERSES, split

    if universe is not None and universe not in UNIVERSES:
        raise HTTPException(status_code=422, detail=f"universe must be one of {UNIVERSES}")
    rows = await get_store().watchlist(
        min_score,
        split(source),
        split(category),
        None if universe == "all" else universe,
    )
    return [WatchlistOut.of(r) for r in rows]


@router.get("/active", response_model=list[ActiveOut])
async def active(
    universe: str = Query("sp500"),
    min_score: int = Query(30, ge=0, le=100),
    range_: str | None = Query(None, alias="range"),
    source: str | None = None,
    category: str | None = None,
    limit: int = Query(10, ge=1, le=50),
) -> list[ActiveOut]:
    """The busiest names in a universe -- the rail, when it cannot list 500 rows.

    Ranked by flags rather than by price move: index names are not on the price
    refresh, so a "change this week" column would be mostly empty.
    """
    from .routes_feed import UNIVERSES, split, window

    if universe not in UNIVERSES or universe == "all":
        raise HTTPException(status_code=422, detail="universe must be core or sp500")
    since, _ = window(range_, None, None)
    rows = await get_store().active_companies(
        universe, min_score, since, split(source), split(category), limit
    )
    return [ActiveOut.of(r) for r in rows]


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
        flag_threshold=await current_flag_threshold(),
        excluded_from_latency=await store.count_excluded_from_latency(window),
    )


@router.get("/system", response_model=list[SystemEventOut])
async def system() -> list[SystemEventOut]:
    """The status strip: open outages first, then the last day's notices."""
    return [SystemEventOut.of(r) for r in await get_store().system_status()]


@router.get("/meta")
async def meta() -> dict[str, object]:
    """Vocabulary for the filter bar, so labels live in one place.

    ``sp500_captured`` is here so a stale membership snapshot is visible rather
    than assumed fresh -- the index changes about twenty names a year.
    """
    from ..seeding import load_sp500

    _members, captured = load_sp500()
    return {
        "sp500_captured": captured.isoformat() if captured else None,
        "categories": [{"value": k, "label": v} for k, v in CATEGORY_LABELS.items()],
        "sources": [
            {"value": "edgar_8k", "label": "8-K"},
            {"value": "edgar_form4", "label": "Form 4"},
            {"value": "edgar_13dg", "label": "13D/G"},
            {"value": "halts", "label": "Halts"},
            {"value": "system", "label": "System"},
        ],
    }


async def current_flag_threshold() -> int:
    stored = await get_store().get_setting("flag_threshold")
    return int(stored) if stored is not None else get_settings().flag_threshold


@router.get("/settings", response_model=SettingsOut)
async def read_settings() -> SettingsOut:
    return SettingsOut(
        flag_threshold=await current_flag_threshold(),
        editable=bool(get_settings().admin_token),
    )


@router.put("/settings", response_model=SettingsOut)
async def write_settings(
    body: SettingsIn, x_admin_token: str | None = Header(default=None)
) -> SettingsOut:
    """Change what gets pushed and price-stamped from now on.

    Nothing stored is touched: every event keeps the score it was given, so
    lowering the threshold later surfaces history that was there all along.
    """
    expected = get_settings().admin_token
    if not expected:
        raise HTTPException(status_code=403, detail="set ADMIN_TOKEN in .env to enable changes")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="wrong or missing admin token")
    if not 0 <= body.flag_threshold <= 100:
        raise HTTPException(status_code=422, detail="flag_threshold must be 0-100")
    await get_store().put_setting("flag_threshold", str(body.flag_threshold))
    return await read_settings()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
