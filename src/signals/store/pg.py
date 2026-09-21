"""asyncpg implementation of Store."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from ..categories import categorize
from ..models import Company, CompanyKey, ResolvedEvent, Score
from . import queries as q
from .base import EventRow, FeedFilter, SourceLatency, WatchlistRow


class PgStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._watched: frozenset[int] | None = None

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> PgStore:
        # A healthy container is not a ready pool, so callers retry around this.
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool.acquire() as conn, conn.transaction():
            yield conn

    # --- resolution --------------------------------------------------------

    async def find_company(self, key: CompanyKey) -> Company | None:
        for kind, value in key.candidates():
            row = await self._pool.fetchrow(q.FIND_COMPANY_BY_ALIAS, kind, value)
            if row is not None:
                return Company(
                    id=row["id"],
                    cik=row["cik"],
                    ticker=row["ticker"],
                    name=row["name"],
                    watched=row["watched"],
                )
        return None

    async def watched_company_ids(self) -> frozenset[int]:
        if self._watched is None:
            rows = await self._pool.fetch(q.WATCHED_COMPANY_IDS)
            self._watched = frozenset(r["id"] for r in rows)
        return self._watched

    def invalidate_watched(self) -> None:
        self._watched = None

    async def watched_tickers(self) -> frozenset[str]:
        rows = await self._pool.fetch(q.WATCHED_TICKERS)
        return frozenset(r["value"] for r in rows)

    async def watched_ciks(self) -> frozenset[str]:
        rows = await self._pool.fetch(q.WATCHED_CIKS)
        return frozenset(r["value"] for r in rows)

    async def record_unresolved(
        self, source: str, external_id: str, raw_name: str, payload: dict[str, Any]
    ) -> None:
        await self._pool.execute(
            q.RECORD_UNRESOLVED, source, external_id, raw_name, json.dumps(payload, default=str)
        )

    # --- write path --------------------------------------------------------

    async def insert_event(self, event: ResolvedEvent) -> int | None:
        n = event.normalized
        payload = dict(n.payload)
        payload["score_parts"] = event.score.parts
        new_id = await self._pool.fetchval(
            q.INSERT_EVENT,
            event.company_id,
            n.source,
            n.event_type,
            n.occurred_at,
            n.external_id,
            n.summary,
            event.score.total,
            json.dumps(payload, default=str),
            n.url,
            categorize(n.source, n.event_type, payload),
        )
        return int(new_id) if new_id is not None else None

    async def stamp_price_at(self, event_id: int, price: Decimal) -> None:
        await self._pool.execute(q.STAMP_PRICE_AT, event_id, price)

    async def merge_payload(
        self, source: str, external_id: str, patch: dict[str, Any], guard_key: str
    ) -> int | None:
        """The row's id on the first merge, None afterwards.

        The guard key makes a repeat merge a genuine no-op, so re-polling neither
        rewrites the row nor re-publishes it.
        """
        row = await self._pool.fetchval(
            q.MERGE_PAYLOAD, source, external_id, json.dumps(patch, default=str), guard_key
        )
        return int(row) if row is not None else None

    async def update_payload(
        self, source: str, external_id: str, patch: dict[str, Any]
    ) -> int | None:
        row = await self._pool.fetchval(
            q.UPDATE_PAYLOAD,
            source,
            external_id,
            json.dumps(patch, default=str),
            patch.get("headline"),
        )
        return int(row) if row is not None else None

    async def update_score(self, event_id: int, score: Score) -> None:
        await self._pool.execute(q.UPDATE_SCORE, event_id, score.total, json.dumps(score.parts))

    # --- cluster promotion -------------------------------------------------

    async def distinct_p_buyers(self, company_id: int, since: datetime) -> int:
        value = await self._pool.fetchval(q.DISTINCT_P_BUYERS, company_id, since)
        return int(value or 0)

    async def events_missing_cluster_bonus(
        self, company_id: int, since: datetime
    ) -> list[EventRow]:
        rows = await self._pool.fetch(q.EVENTS_MISSING_CLUSTER_BONUS, company_id, since)
        return [_row_to_event(r) for r in rows]

    # --- read path ---------------------------------------------------------

    @staticmethod
    def _feed_args(f: FeedFilter) -> tuple[Any, ...]:
        return (
            f.min_score,
            f.since,
            f.until,
            [t.upper() for t in f.tickers] or None,
            list(f.sources) or None,
            list(f.categories) or None,
            # Asking for the system source by name is asking to see it.
            f.include_system or "system" in f.sources,
        )

    async def feed(self, filters: FeedFilter) -> list[EventRow]:
        rows = await self._pool.fetch(
            q.FEED, *self._feed_args(filters), filters.limit, filters.offset
        )
        return [_row_to_event(r) for r in rows]

    async def feed_count(self, filters: FeedFilter) -> int:
        return int(await self._pool.fetchval(q.FEED_COUNT, *self._feed_args(filters)) or 0)

    async def system_status(self) -> list[EventRow]:
        return [_row_to_event(r) for r in await self._pool.fetch(q.SYSTEM_STATUS)]

    async def get_setting(self, key: str) -> str | None:
        value = await self._pool.fetchval(q.GET_SETTING, key)
        return str(value) if value is not None else None

    async def put_setting(self, key: str, value: str) -> None:
        await self._pool.execute(q.PUT_SETTING, key, value)

    async def backfill_categories(self) -> int:
        """Categorize rows stored before the column existed. Idempotent."""
        rows = await self._pool.fetch(q.UNCATEGORIZED)
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            await self._pool.execute(
                q.SET_CATEGORY, r["id"], categorize(r["source"], r["event_type"], payload or {})
            )
        return len(rows)

    async def get_event(self, event_id: int) -> EventRow | None:
        row = await self._pool.fetchrow(q.GET_EVENT, event_id)
        return _row_to_event(row) if row is not None else None

    async def watchlist(self, min_score: int) -> list[WatchlistRow]:
        rows = await self._pool.fetch(q.WATCHLIST, min_score)
        return [
            WatchlistRow(
                company_id=r["id"],
                ticker=r["ticker"],
                name=r["name"],
                flags=r["flags"],
                top_score=r["top"],
                last_event_at=r["last_event_at"],
                last_price=r["last_price"],
                week_ago_price=r["week_ago_price"],
                next_earnings=r["next_earnings"],
            )
            for r in rows
        ]

    async def latency_percentiles(self, window: timedelta) -> list[SourceLatency]:
        rows = await self._pool.fetch(q.LATENCY_PERCENTILES, window)
        return [
            SourceLatency(
                source=r["source"],
                events=r["events"],
                p50=float(r["p50"]) if r["p50"] is not None else None,
                p95=float(r["p95"]) if r["p95"] is not None else None,
            )
            for r in rows
        ]

    async def count_unresolved(self) -> int:
        return int(await self._pool.fetchval(q.COUNT_UNRESOLVED) or 0)

    async def upsert_price_daily(self, company_id: int, d: date, close: Decimal) -> None:
        await self._pool.execute(q.UPSERT_PRICE_DAILY, company_id, d, close)

    async def close_on_or_before(self, company_id: int, d: date) -> Decimal | None:
        value = await self._pool.fetchval(q.CLOSE_ON_OR_BEFORE, company_id, d)
        return Decimal(value) if value is not None else None

    async def set_next_earnings(self, company_id: int, when: date | None) -> None:
        await self._pool.execute(q.SET_NEXT_EARNINGS, company_id, when)

    async def watched_companies(self) -> list[Company]:
        rows = await self._pool.fetch(q.WATCHED_COMPANIES)
        return [
            Company(id=r["id"], cik=r["cik"], ticker=r["ticker"], name=r["name"], watched=True)
            for r in rows
        ]

    async def count_excluded_from_latency(self, window: timedelta) -> int:
        """Backfilled and replayed events, which latency deliberately ignores."""
        return int(await self._pool.fetchval(q.EXCLUDED_FROM_LATENCY, window) or 0)


def _row_to_event(row: asyncpg.Record) -> EventRow:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    # Not every query joins company, so the display columns are optional.
    columns = row.keys()
    return EventRow(
        id=row["id"],
        company_id=row["company_id"],
        source=row["source"],
        event_type=row["event_type"],
        occurred_at=row["occurred_at"],
        ingested_at=row["ingested_at"],
        external_id=row["external_id"],
        summary=row["summary"],
        score=row["score"],
        price_at=row["price_at"],
        payload=payload or {},
        url=row["url"],
        category=row["category"] if "category" in columns else None,
        ticker=row["ticker"] if "ticker" in columns else None,
        company_name=row["company_name"] if "company_name" in columns else None,
        price_now=row["price_now"] if "price_now" in columns else None,
    )
