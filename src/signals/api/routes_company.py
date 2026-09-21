"""Everything about one company, in one request."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..store.base import FeedFilter
from . import deps
from .routes_feed import split, window
from .schemas import EventOut

router = APIRouter()

INSIDER_WINDOW_DAYS = 90
_SUFFIXES = re.compile(
    r"\b(inc|corp|corporation|co|company|ltd|plc|llc|lp|l p|holdings?|group|the|class [a-z])\b\.?",
    re.I,
)


def name_stem(name: str) -> str:
    """The distinctive start of a company name, for matching unresolved filings.

    "Simon Property Group, Inc." -> "simon property". Two significant words, so
    "Apple Inc." does not claim every filer whose name starts with "A".
    """
    cleaned = _SUFFIXES.sub(" ", re.sub(r"[^\w\s]", " ", name.lower()))
    words = [w for w in cleaned.split() if len(w) > 1]
    return " ".join(words[:2])


def insiders_from(rows: list[Any], now: datetime) -> list[dict[str, Any]]:
    """One row per insider, keyed on CIK.

    By CIK and never by name: "John A. Smith" and "SMITH JOHN A" are one person,
    and a name match would split them -- here that would halve their totals, and in
    cluster detection it would invent a cluster.
    """
    cutoff = now - timedelta(days=INSIDER_WINDOW_DAYS)
    people: dict[str, dict[str, Any]] = {}
    for row in rows:
        p = row["payload"]
        ciks = p.get("insider_ciks") or []
        names = p.get("insider_names") or []
        if not ciks:
            continue
        key = ciks[0]
        person = people.setdefault(
            key,
            {
                "cik": key,
                "name": names[0] if names else key,
                "role": p.get("officer_title") or p.get("role") or "Insider",
                "last_codes": p.get("codes") or [],
                "last_date": row["occurred_at"],
                "last_url": row["url"],
                "bought_90d": 0.0,
                "sold_90d": 0.0,
                "filings": 0,
                "plan_10b5_1": bool(p.get("plan_10b5_1")),
                "in_cluster": False,
            },
        )
        person["filings"] += 1
        if row["occurred_at"] >= cutoff:
            person["bought_90d"] += float(p.get("purchase_value") or 0.0)
            person["sold_90d"] += float(p.get("sale_value") or 0.0)
        if (p.get("score_parts") or {}).get("cluster"):
            person["in_cluster"] = True
    return sorted(people.values(), key=lambda x: (-x["bought_90d"], -x["sold_90d"], x["name"]))


@router.get("/company/{ticker}")
async def company(
    ticker: str,
    min_score: int = Query(0, ge=0, le=100),
    source: str | None = None,
    category: str | None = None,
    range_: str | None = Query(None, alias="range"),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Company header, filtered events, price history, insiders, possible aliases.

    Takes the same filters as /api/feed, so the company page and the feed can share
    one filter bar.
    """
    store = deps.get_store()
    row = await store.company_by_ticker(ticker)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no company with ticker {ticker.upper()}")

    start, end = window(range_, since, until)
    filters = FeedFilter(
        min_score=min_score,
        since=start,
        until=end,
        tickers=(row["ticker"],),
        sources=split(source),
        categories=split(category),
        limit=limit,
        offset=offset,
    )
    events = await store.feed(filters)
    total = await store.feed_count(filters)

    now = datetime.now(tz=UTC)
    month = FeedFilter(min_score=1, since=now - timedelta(days=30), tickers=(row["ticker"],))
    prices = await store.company_prices(row["id"], 90)
    form4 = await store.company_form4(row["id"], timedelta(days=365))
    stem = name_stem(row["name"])
    aliases = await store.possible_aliases(stem) if len(stem) >= 5 else []

    closes = [float(close) for _, close in prices]
    week_ago = next(
        (float(close) for d, close in reversed(prices) if d <= now.date() - timedelta(days=7)),
        None,
    )
    return {
        "company": {
            "ticker": row["ticker"],
            "name": row["name"],
            "cik": row["cik"],
            "watched": row["watched"],
            "last_price": closes[-1] if closes else None,
            "week_change": ((closes[-1] - week_ago) / week_ago * 100)
            if closes and week_ago
            else None,
            "next_earnings": row["next_earnings"],
            "flags_this_month": await store.feed_count(month),
        },
        "events": [EventOut.of(e).model_dump(mode="json") for e in events],
        "total": total,
        "prices": [{"d": d.isoformat(), "close": float(close)} for d, close in prices],
        "insiders": insiders_from(form4, now),
        "possible_aliases": [
            {"raw_name": a["raw_name"], "filings": a["filings"], "last_seen": a["last_seen"]}
            for a in aliases
        ],
    }
