"""Universe membership, against a real Postgres.

The feed is a lens over stored events: one event is visible or not depending on
which monitor you are looking through.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from signals.store.base import FeedFilter
from signals.store.pg import PgStore

pytestmark = pytest.mark.pg

NOW = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)


async def company(pg, cik: str, ticker: str, *, universes: tuple[str, ...]) -> int:
    company_id = await pg.fetchval(
        "insert into company (cik, ticker, name, watched) values ($1, $2, $3, true) returning id",
        cik,
        ticker,
        f"{ticker} Inc.",
    )
    await pg.execute(
        "insert into company_alias (company_id, kind, value) values ($1, 'cik', $2)",
        company_id,
        cik,
    )
    for universe in universes:
        await pg.execute(
            "insert into universe_member (company_id, universe) values ($1, $2)",
            company_id,
            universe,
        )
    return company_id


async def event(pg, company_id: int | None, external_id: str, **kw: object) -> None:
    await pg.execute(
        """
        insert into event (company_id, source, event_type, occurred_at, external_id,
                           score, payload, category)
        values ($1, $2, '8k', $3, $4, $5, $6::jsonb, $7)
        """,
        company_id,
        kw.get("source", "edgar_8k"),
        NOW,
        external_id,
        kw.get("score", 55),
        json.dumps({}),
        kw.get("category", "other"),
    )


class TestFeedByUniverse:
    async def test_filters_to_members_of_one_universe(self, pg_dsn: str, pg) -> None:
        core_id = await company(pg, "0000000001", "CORE", universes=("core",))
        sp_id = await company(pg, "0000000002", "SPX", universes=("sp500",))
        await event(pg, core_id, "a")
        await event(pg, sp_id, "b")

        store = await PgStore.connect(pg_dsn)
        try:
            core = await store.feed(FeedFilter(universe="core"))
            sp500 = await store.feed(FeedFilter(universe="sp500"))
            both = await store.feed(FeedFilter())

            assert [r.ticker for r in core] == ["CORE"]
            assert [r.ticker for r in sp500] == ["SPX"]
            assert {r.ticker for r in both} == {"CORE", "SPX"}
            assert await store.feed_count(FeedFilter(universe="sp500")) == 1
        finally:
            await store.close()

    async def test_a_company_in_both_universes_appears_in_both(self, pg_dsn: str, pg) -> None:
        company_id = await company(pg, "0000000003", "DUAL", universes=("core", "sp500"))
        await event(pg, company_id, "c")
        store = await PgStore.connect(pg_dsn)
        try:
            assert await store.feed_count(FeedFilter(universe="core")) == 1
            assert await store.feed_count(FeedFilter(universe="sp500")) == 1
        finally:
            await store.close()

    async def test_system_events_survive_a_universe_filter_when_asked_for(
        self, pg_dsn: str, pg
    ) -> None:
        """A system event belongs to no company, so a plain membership join would
        empty the status strip the moment you switched monitors."""
        await event(pg, None, "outage:edgar_8k", source="system", category="system", score=1)
        store = await PgStore.connect(pg_dsn)
        try:
            hidden = await store.feed(FeedFilter(universe="sp500"))
            shown = await store.feed(FeedFilter(universe="sp500", include_system=True))
            assert hidden == []
            assert [r.source for r in shown] == ["system"]
        finally:
            await store.close()


class TestExcludeCategories:
    async def test_hides_only_the_named_category(self, pg_dsn: str, pg) -> None:
        """What the index view needs by default: every member reports earnings
        once a quarter, which is news you already expected."""
        company_id = await company(pg, "0000000004", "EXCL", universes=("sp500",))
        await event(pg, company_id, "e1", category="earnings")
        await event(pg, company_id, "e2", category="distress")
        store = await PgStore.connect(pg_dsn)
        try:
            rows = await store.feed(
                FeedFilter(universe="sp500", exclude_categories=("earnings",))
            )
            assert [r.category for r in rows] == ["distress"]
        finally:
            await store.close()


class TestUniverseCiks:
    async def test_returns_padded_ciks_for_one_universe(self, pg_dsn: str, pg) -> None:
        await company(pg, "0000000005", "AAA", universes=("core",))
        await company(pg, "0000000006", "BBB", universes=("sp500",))
        store = await PgStore.connect(pg_dsn)
        try:
            assert await store.universe_ciks("core") == frozenset({"0000000005"})
            assert await store.universe_ciks("sp500") == frozenset({"0000000006"})
        finally:
            await store.close()
