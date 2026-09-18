"""A halt's whole life through the processor, against a real Postgres."""

from __future__ import annotations

import asyncpg
import httpx
import pytest

from signals.adapters.base import FetchContext
from signals.adapters.halts import HaltsAdapter
from signals.bus.memory_bus import MemoryBus
from signals.http import SourceClient
from signals.pipeline.process import Processor
from signals.resolve.resolver import Resolver
from signals.store.base import FeedFilter
from signals.store.pg import PgStore
from tests.fakes import FakeClock
from tests.unit.test_halts import UA, feed

pytestmark = pytest.mark.pg


@pytest.fixture
async def rig(pg_dsn: str):
    conn = await asyncpg.connect(pg_dsn)
    try:
        cid = await conn.fetchval(
            "insert into company (cik,ticker,name,watched) "
            "values ('0000000001','RKLB','Rocket Lab',true) returning id"
        )
        await conn.execute(
            "insert into company_alias (company_id,kind,value) values ($1,'ticker','RKLB')", cid
        )
    finally:
        await conn.close()

    store = await PgStore.connect(pg_dsn)
    bus = MemoryBus()
    processor = Processor(store, Resolver(store), bus, flag_threshold=30)
    holder = {"body": b""}
    clock = FakeClock()
    ctx = FetchContext(
        http=SourceClient(
            UA, clock=clock,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=holder["body"])),
        ),
        clock=clock,
    )
    adapter = HaltsAdapter()

    async def poll(items: list[dict[str, str]]) -> None:
        holder["body"] = feed(items)
        for raw in await adapter.fetch(ctx):
            for event in adapter.normalize(raw):
                await processor.process(event)

    yield store, bus, poll
    await store.close()


OPEN = [{"sym": "RKLB"}]
RESUMED = [{"sym": "RKLB", "rdate": "09/15/2026", "quote": "10:00:00", "trade": "10:05:00"}]
QUOTING = [{"sym": "RKLB", "rdate": "09/15/2026", "quote": "10:00:00"}]


async def rows(store: PgStore) -> dict[str, object]:
    return {r.event_type: r for r in await store.feed(FeedFilter(min_score=0))}


async def test_open_then_resumed_is_two_rows(rig) -> None:
    store, _bus, poll = rig
    await poll(OPEN)
    assert set(await rows(store)) == {"halt"}
    await poll(RESUMED)
    assert set(await rows(store)) == {"halt", "halt_resume"}


async def test_the_halt_row_learns_its_resumption(rig) -> None:
    """So the feed can say "halted 09:41, resumes 10:05" on a single row."""
    store, _bus, poll = rig
    await poll(OPEN)
    await poll(RESUMED)
    halt = (await rows(store))["halt"]
    assert halt.payload["resumption"]["trading"] is True
    assert "resumes 10:05" in halt.payload["detail"]


async def test_the_halt_flags_and_the_resume_does_not(rig) -> None:
    store, bus, poll = rig
    await poll(OPEN)
    await poll(RESUMED)
    found = await rows(store)
    assert found["halt"].score == 90
    assert found["halt_resume"].score == 10
    assert [m.type for m in bus.published] == ["event.new", "event.updated"]


async def test_re_polling_changes_and_publishes_nothing(rig) -> None:
    store, bus, poll = rig
    await poll(OPEN)
    await poll(RESUMED)
    bus.clear()
    await poll(RESUMED)
    await poll(RESUMED)
    assert len(await store.feed(FeedFilter(min_score=0))) == 2
    assert bus.published == []


async def test_quotes_then_trading_refines_one_resume_row(rig) -> None:
    """Quotes can resume minutes before trading. The second transition arrives
    as a "duplicate" resume and must still reach the halt row."""
    store, _bus, poll = rig
    await poll(OPEN)
    await poll(QUOTING)
    halt = (await rows(store))["halt"]
    assert halt.payload["resumption"]["trading"] is False
    await poll(RESUMED)
    found = await store.feed(FeedFilter(min_score=0))
    assert len(found) == 2
    halt = (await rows(store))["halt"]
    assert "resumed_trading_at" in halt.payload


async def test_cold_start_stores_the_historical_halt(rig) -> None:
    store, _bus, poll = rig
    await poll(RESUMED)
    found = await rows(store)
    assert set(found) == {"halt", "halt_resume"}
    assert found["halt"].occurred_at < found["halt_resume"].occurred_at


async def test_a_market_wide_halt_is_stored_with_no_company(rig) -> None:
    store, bus, poll = rig
    await poll([{"sym": "ANY", "reason": "MWC1"}])
    halt = (await rows(store))["halt"]
    assert halt.company_id is None
    assert halt.score == 50
    assert len(bus.published) == 1
