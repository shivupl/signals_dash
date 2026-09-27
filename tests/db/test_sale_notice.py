"""A Form 144 through the processor, against a real Postgres.

What matters end to end: the notice resolves to the issuer, scores from its own
contents, and a third seller inside the window turns a quiet notice into a flag.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest

from signals.adapters.base import FetchContext
from signals.adapters.edgar_144 import Edgar144Adapter
from signals.bus.memory_bus import MemoryBus
from signals.http import SourceClient
from signals.pipeline.process import Processor
from signals.resolve.resolver import Resolver
from signals.store.base import FeedFilter
from signals.store.pg import PgStore
from tests.conftest import read_fixture
from tests.fakes import FakeClock

pytestmark = pytest.mark.pg

NOTICE = read_fixture("edgar", "form144_officer_ns2.txt")
UA = "Signals/0.1 (test@example.com)"
ISSUER = "0001943896"  # Rubrik, in the fixture
NOW = datetime(2026, 9, 25, 23, 19, 45, tzinfo=UTC)


def atom(accession: str, seller_cik: str) -> bytes:
    body = "".join(
        f"""<entry><title>{title}</title>
        <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1/{accession}-index.htm"/>
        <summary type="html">AccNo: {accession}</summary>
        <updated>2026-09-25T19:19:45-04:00</updated>
        <id>urn:tag:sec.gov,2008:accession-number={accession}</id></entry>"""
        for title in (
            f"144 - Rubrik, Inc. ({ISSUER}) (Subject)",
            f"144 - A Seller ({seller_cik}) (Reporting)",
        )
    )
    return (
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'
    ).encode()


@pytest.fixture
async def rig(pg_dsn: str):
    conn = await asyncpg.connect(pg_dsn)
    try:
        cid = await conn.fetchval(
            "insert into company (cik,ticker,name,watched) "
            "values ($1,'RBRK','Rubrik, Inc.',true) returning id",
            ISSUER,
        )
        await conn.execute(
            "insert into company_alias (company_id,kind,value) values ($1,'cik',$2)", cid, ISSUER
        )
    finally:
        await conn.close()

    store = await PgStore.connect(pg_dsn)
    clock = FakeClock(NOW)
    processor = Processor(store, Resolver(store), MemoryBus(), flag_threshold=30, clock=clock)
    holder = {"index": b"", "doc": NOTICE}

    def handler(request: httpx.Request) -> httpx.Response:
        if "browse-edgar" in str(request.url):
            return httpx.Response(200, content=holder["index"])
        return httpx.Response(200, content=holder["doc"])

    ctx = FetchContext(
        http=SourceClient(UA, clock=clock, transport=httpx.MockTransport(handler)),
        clock=clock,
        watched_ciks=frozenset({ISSUER}),
        state={},
    )
    adapter = Edgar144Adapter()

    async def ingest(accession: str, seller_cik: str) -> None:
        holder["index"] = atom(accession, seller_cik)
        ctx.state.pop("digest:144", None)
        for raw in await adapter.fetch(ctx):
            for event in adapter.normalize(raw):
                await processor.process(event)

    yield store, ingest, cid
    await store.close()


class TestOneNotice:
    async def test_it_resolves_to_the_issuer_and_is_recorded(self, rig) -> None:
        store, ingest, cid = rig
        await ingest("0001958244-26-000624", "0001685768")
        rows = await store.feed(FeedFilter(min_score=0))
        assert len(rows) == 1
        row = rows[0]
        assert row.company_id == cid
        assert row.source == "edgar_144"
        assert row.category == "sale_notice"
        assert row.payload["value"] == 56_900_000.0

    async def test_a_large_officer_notice_flags_on_the_watchlist(self, rig) -> None:
        """$56.9M by an officer: 45, over the watchlist's 30 and under the index's
        50. Recorded loudly enough to see, quietly enough not to dominate."""
        store, ingest, _cid = rig
        await ingest("0001958244-26-000624", "0001685768")
        row = (await store.feed(FeedFilter(min_score=0)))[0]
        assert row.score == 45
        assert row.payload["headline"] == "Planned sale by Bipul Sinha · $56,900,000"

    async def test_re_ingesting_the_same_notice_stores_one_row(self, rig) -> None:
        store, ingest, _cid = rig
        await ingest("0001958244-26-000624", "0001685768")
        await ingest("0001958244-26-000624", "0001685768")
        assert await store.feed_count(FeedFilter(min_score=0)) == 1


class TestCluster:
    async def test_a_third_seller_scores_as_a_cluster(self, rig) -> None:
        store, ingest, _cid = rig
        for n, seller in enumerate(("0000000001", "0000000002", "0000000003")):
            await ingest(f"0001958244-26-00062{n}", seller)

        rows = sorted(await store.feed(FeedFilter(min_score=0)), key=lambda r: r.external_id)
        assert len(rows) == 3
        assert [r.score for r in rows] == [45, 45, 70], (
            "the third notice sees the crowd; the earlier two keep their scores, "
            "because notices are not retroactively promoted"
        )
        assert rows[-1].payload["score_parts"]["cluster"] == 25
        assert "3 insiders selling in 30 days" in rows[-1].payload["detail"]

    async def test_the_same_seller_filing_twice_is_not_a_cluster(self, rig) -> None:
        """By CIK: one person filing three notices is one person."""
        store, ingest, _cid = rig
        for n in range(3):
            await ingest(f"0001958244-26-00072{n}", "0000000001")
        rows = await store.feed(FeedFilter(min_score=0))
        assert {r.score for r in rows} == {45}

    async def test_sellers_outside_the_window_do_not_count(self, rig, pg_dsn: str) -> None:
        store, ingest, cid = rig
        conn = await asyncpg.connect(pg_dsn)
        try:
            for n, seller in enumerate(("0000000007", "0000000008")):
                await conn.execute(
                    """
                    insert into event (company_id, source, event_type, occurred_at, external_id,
                                       score, payload, category)
                    values ($1, 'edgar_144', 'sale_notice', $2, $3, 20,
                            jsonb_build_object('seller_ciks', jsonb_build_array($4::text)),
                            'sale_notice')
                    """,
                    cid,
                    NOW - timedelta(days=45),
                    f"old-{n}",
                    seller,
                )
        finally:
            await conn.close()
        await ingest("0001958244-26-000624", "0001685768")
        rows = await store.feed(FeedFilter(min_score=0))
        fresh = [r for r in rows if r.external_id.startswith("0001")]
        assert fresh[0].score == 45, "a 45-day-old notice is not part of today's crowd"
