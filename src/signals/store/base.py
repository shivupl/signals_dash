"""The database surface the rest of the application sees."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from ..models import Company, CompanyKey, ResolvedEvent, Score


@dataclass(frozen=True, slots=True)
class EventRow:
    id: int
    company_id: int | None
    source: str
    event_type: str
    occurred_at: datetime
    ingested_at: datetime
    external_id: str
    summary: str | None
    score: int
    price_at: Decimal | None
    payload: dict[str, Any]
    url: str | None
    category: str | None = None
    ticker: str | None = None
    company_name: str | None = None
    price_now: Decimal | None = None

    @property
    def change_since(self) -> float | None:
        """Percent move since the flag fired. Annotation, not advice."""
        if not self.price_at or not self.price_now:
            return None
        return float((self.price_now - self.price_at) / self.price_at * 100)

    @property
    def latency_seconds(self) -> float:
        """Detection lag: how long between publication and our knowing."""
        return (self.ingested_at - self.occurred_at).total_seconds()


@dataclass(frozen=True, slots=True)
class WatchlistRow:
    company_id: int
    ticker: str | None
    name: str
    flags: int
    top_score: int
    last_event_at: datetime | None
    last_price: Decimal | None = None
    week_ago_price: Decimal | None = None
    next_earnings: date | None = None

    @property
    def week_change(self) -> float | None:
        if not self.last_price or not self.week_ago_price:
            return None
        return float((self.last_price - self.week_ago_price) / self.week_ago_price * 100)


@dataclass(frozen=True, slots=True)
class ActiveRow:
    """A busy company in a universe. No price: index names are not refreshed."""

    company_id: int
    ticker: str | None
    name: str
    flags: int
    top_score: int
    last_event_at: datetime | None


@dataclass(frozen=True, slots=True)
class SourceLatency:
    source: str
    events: int
    p50: float | None
    p95: float | None


@dataclass(frozen=True, slots=True)
class FeedFilter:
    min_score: int = 0
    since: datetime | None = None
    until: datetime | None = None
    tickers: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    #: System events are the pipeline talking about itself. Off by default.
    include_system: bool = False
    #: Which monitor is being looked through: 'core', 'sp500', or None for every
    #: company. A lens over stored events -- it never changes what gets ingested.
    universe: str | None = None
    #: Categories to leave out. An include-list cannot say "everything except
    #: earnings", which is what the index view needs by default.
    exclude_categories: tuple[str, ...] = ()
    limit: int = 200
    offset: int = 0


class Store(Protocol):
    async def find_company(self, key: CompanyKey) -> Company | None: ...
    async def watched_company_ids(self) -> frozenset[int]: ...
    async def watched_ciks(self) -> frozenset[str]: ...
    async def universe_ciks(self, universe: str) -> frozenset[str]: ...
    async def watched_tickers(self) -> frozenset[str]: ...
    async def record_unresolved(
        self, source: str, external_id: str, raw_name: str, payload: dict[str, Any]
    ) -> None: ...

    #: Returns the new row's id, or None when the event was already stored.
    #: Expressing dedupe in the return type means a caller cannot forget it.
    async def insert_event(self, event: ResolvedEvent) -> int | None: ...
    async def stamp_price_at(self, event_id: int, price: Decimal) -> None: ...
    async def merge_payload(
        self, source: str, external_id: str, patch: dict[str, Any], guard_key: str
    ) -> int | None: ...

    async def distinct_p_buyers(self, company_id: int, since: datetime) -> int: ...
    async def events_missing_cluster_bonus(
        self, company_id: int, since: datetime
    ) -> list[EventRow]: ...
    async def update_score(self, event_id: int, score: Score) -> None: ...
    async def update_payload(
        self, source: str, external_id: str, patch: dict[str, Any]
    ) -> int | None: ...

    async def feed(self, filters: FeedFilter) -> list[EventRow]: ...
    async def feed_count(self, filters: FeedFilter) -> int: ...
    async def system_status(self) -> list[EventRow]: ...
    async def get_setting(self, key: str) -> str | None: ...
    async def put_setting(self, key: str, value: str) -> None: ...
    async def get_event(self, event_id: int) -> EventRow | None: ...
    async def watchlist(
        self,
        min_score: int,
        sources: tuple[str, ...] = (),
        categories: tuple[str, ...] = (),
        universe: str | None = None,
    ) -> list[WatchlistRow]: ...
    async def active_companies(
        self,
        universe: str,
        min_score: int,
        since: datetime | None = None,
        sources: tuple[str, ...] = (),
        categories: tuple[str, ...] = (),
        limit: int = 10,
    ) -> list[ActiveRow]: ...
    async def latency_percentiles(self, window: timedelta) -> list[SourceLatency]: ...
    async def count_unresolved(self) -> int: ...
    async def upsert_price_daily(self, company_id: int, d: date, close: Decimal) -> None: ...
    async def close_on_or_before(self, company_id: int, d: date) -> Decimal | None: ...
    async def set_next_earnings(self, company_id: int, when: date | None) -> None: ...
    async def watched_companies(self) -> list[Company]: ...
