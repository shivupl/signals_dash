"""Prices are annotation. The property that matters: they can never block a flag."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest

import signals.prices as prices_module
from signals.bus.memory_bus import MemoryBus
from signals.clock import MarketCalendar
from signals.models import CompanyKey, NormalizedEvent
from signals.pipeline.price_loop import refresh_earnings, refresh_prices, run_price_loop
from signals.pipeline.process import Processor
from signals.prices import PriceService
from signals.resolve.resolver import Resolver
from signals.store.base import FeedFilter
from signals.store.pg import PgStore
from tests.fakes import FakeClock

NOW = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)


class FakeProvider:
    def __init__(
        self, *, price: str | None = "63.55", fail: bool = False, delay: float = 0
    ) -> None:
        self.price, self.fail, self.delay = price, fail, delay
        self.quotes = 0

    def quote(self, ticker: str) -> Decimal | None:
        self.quotes += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("yahoo changed something again")
        return Decimal(self.price) if self.price else None

    def daily_closes(self, tickers: Sequence[str], days: int) -> dict[str, dict[date, Decimal]]:
        if self.fail:
            raise RuntimeError("down")
        return {
            t: {date(2026, 9, 10): Decimal("60.00"), date(2026, 9, 17): Decimal("66.00")}
            for t in tickers
        }

    def next_earnings(self, ticker: str) -> date | None:
        return date(2026, 10, 29)


class TestPriceService:
    async def test_returns_the_quote(self) -> None:
        assert await PriceService(FakeProvider()).quote("RKLB") == Decimal("63.55")

    async def test_a_provider_exception_becomes_none(self) -> None:
        assert await PriceService(FakeProvider(fail=True)).quote("RKLB") is None

    async def test_a_hung_provider_is_cut_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(prices_module, "QUOTE_TIMEOUT", 0.05)
        started = time.monotonic()
        assert await PriceService(FakeProvider(delay=0.5)).quote("RKLB") is None
        assert time.monotonic() - started < 0.4

    async def test_bulk_failure_is_an_empty_result(self) -> None:
        assert await PriceService(FakeProvider(fail=True)).daily_closes(["RKLB"]) == {}


@pytest.fixture
async def rig(pg_dsn: str):
    conn = await asyncpg.connect(pg_dsn)
    try:
        cid = await conn.fetchval(
            "insert into company (cik,ticker,name,watched) "
            "values ('0000000001','RKLB','Rocket Lab',true) returning id"
        )
        await conn.execute(
            "insert into company_alias (company_id,kind,value) values ($1,'cik','0000000001')", cid
        )
    finally:
        await conn.close()
    store = await PgStore.connect(pg_dsn)
    yield store, cid
    await store.close()


def flag() -> NormalizedEvent:
    return NormalizedEvent(
        source="edgar_8k",
        external_id="acc-1",
        event_type="8k",
        occurred_at=NOW,
        company_key=CompanyKey(cik="0000000001"),
        summary="x",
        payload={"items": ["4.02"]},
    )


@pytest.mark.pg
class TestStamping:
    async def test_a_flag_gets_its_price(self, rig) -> None:
        store, _ = rig
        bus = MemoryBus()
        processor = Processor(
            store,
            Resolver(store),
            bus,
            flag_threshold=30,
            clock=FakeClock(NOW),
            prices=PriceService(FakeProvider()),
        )
        await processor.process(flag())
        await processor.drain()
        row = (await store.feed(FeedFilter()))[0]
        assert row.price_at == Decimal("63.55")
        assert [m.type for m in bus.published] == ["event.new", "event.updated"]

    async def test_the_flag_goes_out_before_the_price_is_even_requested(self, rig) -> None:
        store, _ = rig
        bus = MemoryBus()
        provider = FakeProvider(delay=0.3)
        processor = Processor(
            store,
            Resolver(store),
            bus,
            flag_threshold=30,
            clock=FakeClock(NOW),
            prices=PriceService(provider),
        )
        started = time.monotonic()
        await processor.process(flag())
        assert time.monotonic() - started < 0.25, "the flag waited on the price"
        assert [m.type for m in bus.published] == ["event.new"]
        await processor.drain()

    async def test_a_dead_price_source_costs_a_blank_and_nothing_else(self, rig) -> None:
        store, _ = rig
        bus = MemoryBus()
        processor = Processor(
            store,
            Resolver(store),
            bus,
            flag_threshold=30,
            clock=FakeClock(NOW),
            prices=PriceService(FakeProvider(fail=True)),
        )
        stats = await processor.process(flag())
        await processor.drain()
        assert stats.published == 1
        assert (await store.feed(FeedFilter()))[0].price_at is None

    async def test_price_at_is_written_once_and_never_overwritten(self, rig) -> None:
        """It means "the price when the flag fired". A later write would quietly
        turn it into something else."""
        store, _ = rig
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=30,
            clock=FakeClock(NOW),
            prices=PriceService(FakeProvider()),
        )
        await processor.process(flag())
        await processor.drain()
        event_id = (await store.feed(FeedFilter()))[0].id
        await store.stamp_price_at(event_id, Decimal("999"))
        assert (await store.get_event(event_id)).price_at == Decimal("63.55")

    async def test_sub_threshold_events_are_not_priced(self, rig) -> None:
        store, _ = rig
        provider = FakeProvider()
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=99,
            clock=FakeClock(NOW),
            prices=PriceService(provider),
        )
        await processor.process(flag())
        await processor.drain()
        assert provider.quotes == 0


@pytest.mark.pg
class TestLateDiscovery:
    """price_at means the price AT THE EVENT, not when we happened to notice it.

    Found live: the reconciliation sweep surfaced a director's purchase from three
    days earlier and stamped it with that afternoon's quote, 18.11. The stock had
    closed at 15.25 on the day of the purchase -- so the feed read "0.0% since"
    when the truth was +18.8%.
    """

    async def test_an_old_event_is_priced_at_its_own_close(self, rig) -> None:
        store, cid = rig
        await store.upsert_price_daily(cid, NOW.date(), Decimal("15.25"))
        provider = FakeProvider(price="18.11")
        three_days_later = FakeClock(NOW + timedelta(days=3))
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=30,
            clock=three_days_later,
            prices=PriceService(provider),
        )
        await processor.process(flag())
        await processor.drain()
        assert (await store.feed(FeedFilter()))[0].price_at == Decimal("15.25")
        assert provider.quotes == 0, "a live quote was requested for a stale event"

    async def test_a_weekend_filing_takes_the_prior_close(self, rig) -> None:
        store, cid = rig
        await store.upsert_price_daily(cid, NOW.date() - timedelta(days=2), Decimal("14.00"))
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=30,
            clock=FakeClock(NOW + timedelta(days=3)),
            prices=PriceService(FakeProvider()),
        )
        await processor.process(flag())
        await processor.drain()
        assert (await store.feed(FeedFilter()))[0].price_at == Decimal("14.00")

    async def test_no_close_on_record_leaves_it_blank_rather_than_wrong(self, rig) -> None:
        store, _ = rig
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=30,
            clock=FakeClock(NOW + timedelta(days=3)),
            prices=PriceService(FakeProvider()),
        )
        await processor.process(flag())
        await processor.drain()
        assert (await store.feed(FeedFilter()))[0].price_at is None

    async def test_a_fresh_event_still_takes_the_live_quote(self, rig) -> None:
        store, _ = rig
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=30,
            clock=FakeClock(NOW + timedelta(minutes=2)),
            prices=PriceService(FakeProvider(price="63.55")),
        )
        await processor.process(flag())
        await processor.drain()
        assert (await store.feed(FeedFilter()))[0].price_at == Decimal("63.55")


@pytest.mark.pg
class TestRefresh:
    async def test_closes_are_written_and_reads_derive_from_them(self, rig) -> None:
        store, _cid = rig
        assert await refresh_prices(store, PriceService(FakeProvider())) == 2
        rail = (await store.watchlist(30))[0]
        assert rail.last_price == Decimal("66.00")

    async def test_week_change(self, rig, pg_dsn: str) -> None:
        store, cid = rig
        today = datetime.now(tz=UTC).date()
        await store.upsert_price_daily(cid, today - timedelta(days=8), Decimal("50"))
        await store.upsert_price_daily(cid, today, Decimal("55"))
        assert (await store.watchlist(30))[0].week_change == pytest.approx(10.0)

    async def test_change_since_the_flag(self, rig) -> None:
        store, cid = rig
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=30,
            clock=FakeClock(NOW),
            prices=PriceService(FakeProvider(price="100")),
        )
        await processor.process(flag())
        await processor.drain()
        await store.upsert_price_daily(cid, datetime.now(tz=UTC).date(), Decimal("91.8"))
        assert (await store.feed(FeedFilter()))[0].change_since == pytest.approx(-8.2)

    async def test_refreshing_twice_is_idempotent(self, rig) -> None:
        store, _ = rig
        svc = PriceService(FakeProvider())
        await refresh_prices(store, svc)
        await refresh_prices(store, svc)
        n = await store._pool.fetchval("select count(*) from price_daily")
        assert n == 2

    async def test_earnings_dates_land_on_the_company(self, rig) -> None:
        store, _ = rig
        assert await refresh_earnings(store, PriceService(FakeProvider())) == 1
        assert (await store.watchlist(30))[0].next_earnings == date(2026, 10, 29)

    async def test_a_failing_provider_does_not_stop_the_loop(self, rig) -> None:
        store, _ = rig
        clock = FakeClock()
        await run_price_loop(
            store,
            PriceService(FakeProvider(fail=True)),
            clock,
            MarketCalendar.load(),
            max_iterations=3,
        )
        assert len(clock.sleeps) == 3
