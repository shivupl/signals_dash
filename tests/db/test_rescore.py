"""Rescoring stored events with the current rules, against a real Postgres.

The property that matters: a rescored event is identical to what a fresh ingest
would produce today. If those two paths can disagree, the corpus stops being a
baseline and becomes a second opinion.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import asyncpg
import pytest

from signals.rescore import rescore, rescore_row
from signals.store.base import FeedFilter
from signals.store.pg import PgStore

pytestmark = pytest.mark.pg

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


async def insert(pg: asyncpg.Connection, **kw: object) -> int:
    company_id = await pg.fetchval(
        "insert into company (cik, ticker, name, watched) values ($1,$2,$3,true) "
        "on conflict (cik) do update set ticker = excluded.ticker returning id",
        kw.get("cik", "0000000055"),
        kw.get("ticker", "TEST"),
        "Test Corp",
    )
    return int(
        await pg.fetchval(
            """
            insert into event (company_id, source, event_type, occurred_at, external_id,
                               score, payload, category)
            values ($1, $2, $3, $4, $5, $6, $7::jsonb, 'other') returning id
            """,
            company_id,
            kw["source"],
            kw["event_type"],
            NOW,
            kw["external_id"],
            kw["score"],
            json.dumps(kw["payload"]),
        )
    )


class TestRescoreRow:
    async def test_a_large_sale_gains_a_score_it_never_had(self, pg_dsn: str, pg) -> None:
        """The $465M case: stored at 0 under the old rules, 60 under the new."""
        await insert(
            pg,
            source="edgar_form4",
            event_type="form4_other",
            external_id="big-sale",
            score=0,
            payload={"sale_value": 465_450_000, "codes": ["S"], "plan_10b5_1": False},
        )
        store = await PgStore.connect(pg_dsn)
        try:
            row = (await store.feed(FeedFilter(min_score=0)))[0]
            assert row.score == 0
            assert rescore_row(row).total == 60
        finally:
            await store.close()

    async def test_a_routine_notice_loses_its_flag(self, pg_dsn: str, pg) -> None:
        await insert(
            pg,
            source="edgar_144",
            event_type="sale_notice",
            external_id="routine",
            score=30,
            payload={"value": 400_000, "is_insider": True, "percent_outstanding": 0.01},
        )
        store = await PgStore.connect(pg_dsn)
        try:
            row = (await store.feed(FeedFilter(min_score=0)))[0]
            assert rescore_row(row).total == 25, "below the bar, recorded not flagged"
        finally:
            await store.close()

    async def test_the_cluster_count_is_read_back_not_recounted(self, pg_dsn: str, pg) -> None:
        """The crowd around an event is a fact about when it happened. Recounting
        today would rewrite history with a number from a different month."""
        await insert(
            pg,
            source="edgar_form4",
            event_type="form4_buy",
            external_id="clustered",
            score=65,
            payload={
                "is_open_market_purchase": True,
                "purchase_value": 500_000,
                "cluster_insiders": 3,
            },
        )
        store = await PgStore.connect(pg_dsn)
        try:
            row = (await store.feed(FeedFilter(min_score=0)))[0]
            assert rescore_row(row).parts.get("cluster") == 25
        finally:
            await store.close()

    async def test_an_eightk_is_unchanged_by_this_work(self, pg_dsn: str, pg) -> None:
        await insert(
            pg,
            source="edgar_8k",
            event_type="8k",
            external_id="earnings",
            score=60,
            payload={"items": ["2.02"]},
        )
        store = await PgStore.connect(pg_dsn)
        try:
            row = (await store.feed(FeedFilter(min_score=0)))[0]
            assert rescore_row(row).total == 60
        finally:
            await store.close()


class TestRescorePass:
    async def test_dry_run_writes_nothing(self, pg_dsn: str, pg) -> None:
        await insert(
            pg,
            source="edgar_144",
            event_type="sale_notice",
            external_id="dry",
            score=30,
            payload={"value": 400_000, "is_insider": True},
        )
        changes = await rescore(pg_dsn, threshold=30, dry_run=True)
        assert changes.changed == 1
        assert changes.flags_before == 1
        assert changes.flags_after == 0
        assert await pg.fetchval("select score from event where external_id = 'dry'") == 30

    async def test_a_real_pass_writes_and_is_idempotent(self, pg_dsn: str, pg) -> None:
        await insert(
            pg,
            source="edgar_144",
            event_type="sale_notice",
            external_id="wet",
            score=30,
            payload={"value": 400_000, "is_insider": True},
        )
        first = await rescore(pg_dsn, threshold=30, dry_run=False)
        assert first.changed == 1
        assert await pg.fetchval("select score from event where external_id = 'wet'") == 25

        second = await rescore(pg_dsn, threshold=30, dry_run=False)
        assert second.changed == 0, "rescoring twice must be a no-op"

    async def test_the_headline_is_rewritten_with_the_score(self, pg_dsn: str, pg) -> None:
        """A new number beside the old sentence is how a feed lies quietly."""
        await insert(
            pg,
            source="edgar_144",
            event_type="sale_notice",
            external_id="described",
            score=30,
            payload={
                "value": 56_900_000,
                "is_insider": True,
                "seller_name": "Sinha Bipul",
                "relationship": "Officer",
                "headline": "stale",
                "detail": "stale",
            },
        )
        await rescore(pg_dsn, threshold=30, dry_run=False)
        payload = json.loads(
            await pg.fetchval("select payload::text from event where external_id = 'described'")
        )
        assert payload["headline"] == "Planned sale by Sinha Bipul · $56,900,000"
        assert "stale" not in payload["detail"]

    async def test_system_events_are_left_alone(self, pg_dsn: str, pg) -> None:
        await pg.execute(
            """
            insert into event (source, event_type, occurred_at, external_id, score, payload)
            values ('system', 'stale', $1, 'outage:x', 50, '{"severity": 50}'::jsonb)
            """,
            NOW,
        )
        changes = await rescore(pg_dsn, threshold=30, dry_run=False)
        assert changes.seen == 0, "the pipeline's own events are not scored by rules"
        assert await pg.fetchval("select score from event where external_id = 'outage:x'") == 50
