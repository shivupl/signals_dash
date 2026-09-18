"""Cluster promotion, against a real Postgres.

The specific case this exists for: three 10b5-1 purchases from distinct insiders.
Each scores 40 - 30 = 10 and is stored without being flagged. The third one turns
all three into a cluster, lifting them to 35 -- across the threshold of 30. An
event correctly ignored an hour ago becomes worth reading, and nothing else in
the pipeline can make that happen.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from signals.bus.memory_bus import MemoryBus
from signals.pipeline.promote import Promoter, count_cluster_insiders
from signals.store.base import FeedFilter
from signals.store.pg import PgStore

pytestmark = pytest.mark.pg

NOW = datetime(2026, 9, 17, 20, 0, tzinfo=UTC)


@pytest.fixture
async def store(pg_dsn: str):
    s = await PgStore.connect(pg_dsn)
    yield s
    await s.close()


@pytest.fixture
async def company(pg_dsn: str) -> int:
    conn = await asyncpg.connect(pg_dsn)
    try:
        return await conn.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000001','TEST','Test Corp', true) returning id"
        )
    finally:
        await conn.close()


async def add_buy(
    pg_dsn: str,
    company_id: int,
    *,
    accession: str,
    insider_cik: str,
    days_ago: float = 0,
    plan: bool = True,
    score: int = 10,
    parts: dict | None = None,
) -> int:
    conn = await asyncpg.connect(pg_dsn)
    try:
        payload = {
            "is_open_market_purchase": True,
            "purchase_shares": 1000.0,
            "purchase_value": 50_000.0,
            "insider_ciks": [insider_cik],
            "plan_10b5_1": plan,
            "score_parts": parts if parts is not None else {"base": 40, "plan_10b5_1": -30},
        }
        return await conn.fetchval(
            """
            insert into event (company_id, source, event_type, occurred_at,
                               external_id, summary, score, payload)
            values ($1,'edgar_form4','form4_buy',$2,$3,'buy',$4,$5::jsonb)
            returning id
            """,
            company_id,
            NOW - timedelta(days=days_ago),
            accession,
            score,
            json.dumps(payload),
        )
    finally:
        await conn.close()


class TestClusterCounting:
    async def test_counts_distinct_insiders_not_filings(self, store, pg_dsn, company) -> None:
        """One insider buying three times is not three insiders buying."""
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik="0000000111")
        assert await count_cluster_insiders(store, company, NOW) == 1

    async def test_three_distinct_insiders_is_a_cluster(self, store, pg_dsn, company) -> None:
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        assert await count_cluster_insiders(store, company, NOW) == 3

    async def test_buys_outside_the_window_do_not_count(self, store, pg_dsn, company) -> None:
        await add_buy(pg_dsn, company, accession="old", insider_cik="0000000111", days_ago=31)
        await add_buy(pg_dsn, company, accession="new", insider_cik="0000000222")
        assert await count_cluster_insiders(store, company, NOW) == 1

    async def test_a_joint_filing_contributes_every_owner(self, store, pg_dsn, company) -> None:
        conn = await asyncpg.connect(pg_dsn)
        try:
            await conn.execute(
                """
                insert into event (company_id, source, event_type, occurred_at,
                                   external_id, summary, score, payload)
                values ($1,'edgar_form4','form4_buy',$2,'joint','buy',40,$3::jsonb)
                """,
                company,
                NOW,
                json.dumps({"insider_ciks": ["0000000111", "0000000222", "0000000333"]}),
            )
        finally:
            await conn.close()
        assert await count_cluster_insiders(store, company, NOW) == 3


class TestPromotion:
    async def test_the_threshold_crossing_case(self, store, pg_dsn, company) -> None:
        ids = [
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}",
                          days_ago=10 - i * 5)
            for i in range(3)
        ]
        bus = MemoryBus()
        result = await Promoter(store, bus, flag_threshold=30).promote(company, NOW)

        assert result.cluster_insiders == 3
        assert len(result.promoted) == 3
        for event_id in ids:
            row = await store.get_event(event_id)
            assert row is not None
            assert row.score == 35, "10b5-1 buy did not reach the flag threshold"
            assert row.payload["score_parts"]["cluster"] == 25

    async def test_every_promoted_row_is_announced(self, store, pg_dsn, company) -> None:
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        bus = MemoryBus()
        await Promoter(store, bus, flag_threshold=30).promote(company, NOW)

        assert len(bus.published) == 3
        assert {m.type for m in bus.published} == {"event.updated"}

    async def test_the_message_carries_the_whole_event(self, store, pg_dsn, company) -> None:
        """A client filtering at min_score never received the score-10 version,
        so it must be able to insert from the update, not apply a delta."""
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        bus = MemoryBus()
        await Promoter(store, bus, flag_threshold=30).promote(company, NOW)

        data = bus.published[0].data
        assert data["score"] == 35
        assert data["ticker"] == "TEST"
        assert data["headline"]

    async def test_newly_flagged_rows_are_identified(self, store, pg_dsn, company) -> None:
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        result = await Promoter(store, MemoryBus(), flag_threshold=30).promote(company, NOW)
        assert len(result.newly_flagged) == 3

    async def test_two_insiders_is_not_enough(self, store, pg_dsn, company) -> None:
        for i in range(2):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        bus = MemoryBus()
        result = await Promoter(store, bus, flag_threshold=30).promote(company, NOW)
        assert result.promoted == []
        assert bus.published == []


class TestIdempotence:
    async def test_running_twice_changes_nothing(self, store, pg_dsn, company) -> None:
        """The guard is the absence of a cluster part, so a second pass finds no
        work rather than redoing it."""
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        bus = MemoryBus()
        promoter = Promoter(store, bus, flag_threshold=30)

        first = await promoter.promote(company, NOW)
        bus.clear()
        second = await promoter.promote(company, NOW)

        assert len(first.promoted) == 3
        assert second.promoted == []
        assert bus.published == []

    async def test_an_already_promoted_row_is_not_touched_again(
        self, store, pg_dsn, company
    ) -> None:
        await add_buy(pg_dsn, company, accession="done", insider_cik="0000000111",
                      score=35, parts={"base": 40, "plan_10b5_1": -30, "cluster": 25})
        for i in range(2):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000022{i}")

        result = await Promoter(store, MemoryBus(), flag_threshold=30).promote(company, NOW)
        assert {r.external_id for r in result.promoted} == {"a0", "a1"}

    async def test_a_fourth_buy_from_a_repeat_insider_promotes_nothing_new(
        self, store, pg_dsn, company
    ) -> None:
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        promoter = Promoter(store, MemoryBus(), flag_threshold=30)
        await promoter.promote(company, NOW)

        await add_buy(pg_dsn, company, accession="a4", insider_cik="0000000110",
                      score=35, parts={"base": 40, "plan_10b5_1": -30, "cluster": 25})
        again = await promoter.promote(company, NOW)
        assert again.promoted == []


class TestOneWayPromotion:
    async def test_an_aged_out_row_keeps_its_promoted_score(
        self, store, pg_dsn, company
    ) -> None:
        """Scores record what was known when the flag fired. Demoting an event
        because its cluster later aged out would rewrite history."""
        for i in range(3):
            await add_buy(pg_dsn, company, accession=f"a{i}", insider_cik=f"000000011{i}")
        promoter = Promoter(store, MemoryBus(), flag_threshold=30)
        await promoter.promote(company, NOW)

        later = NOW + timedelta(days=45)
        result = await promoter.promote(company, later)

        assert result.cluster_insiders == 0
        rows = await store.feed(FeedFilter(min_score=0))
        assert {r.score for r in rows} == {35}
