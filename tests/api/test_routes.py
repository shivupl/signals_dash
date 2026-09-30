"""API routes, against a real Postgres through the ASGI transport."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from pytest_socket import enable_socket

from signals.api import deps
from signals.api.app import create_app
from signals.api.schemas import tier_for
from signals.config import Settings
from signals.store.pg import PgStore

pytestmark = pytest.mark.pg

# Anchored to the current time, not a fixed date: the rail counts the last seven
# days, so a hard-coded timestamp made this file pass in September and fail in
# October. Nothing here asserts an absolute date.
NOW = datetime.now(tz=UTC) - timedelta(hours=3)


async def _seed_events(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("delete from event")
        await conn.execute("delete from company_alias")
        await conn.execute("delete from company")
        company_id = await conn.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000001','TEST','Test Corp', true) returning id"
        )
        await conn.execute(
            "insert into company_alias (company_id, kind, value) values ($1,'ticker','TEST')",
            company_id,
        )
        rows = [
            ("restatement", 95, NOW),
            ("earnings", 60, NOW - timedelta(hours=1)),
            ("routine", 20, NOW - timedelta(hours=2)),
        ]
        for external_id, score, occurred in rows:
            await conn.execute(
                """
                insert into event (company_id, source, event_type, occurred_at,
                                   external_id, summary, score, payload, url)
                values ($1,'edgar_8k','8k',$2,$3,$4,$5,$6::jsonb,'https://example.com')
                """,
                company_id,
                occurred,
                external_id,
                f"summary {external_id}",
                score,
                json.dumps({"score_parts": {"base": score}}),
            )
    finally:
        await conn.close()


@pytest.fixture
async def client(pg_dsn: str):
    enable_socket()
    await _seed_events(pg_dsn)
    store = await PgStore.connect(pg_dsn)
    deps.set_store(store)
    deps.set_settings(
        Settings.from_env(
            {"DATABASE_URL": pg_dsn, "FLAG_THRESHOLD": "30"}, require_sec_user_agent=False
        )
    )
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await store.close()
    deps.set_store(None)


class TestFeed:
    async def test_returns_everything_by_default(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed")).json()
        assert len(rows) == 3

    async def test_newest_first(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed")).json()
        stamps = [r["occurred_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)

    async def test_min_score_filters(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed?min_score=60")).json()
        assert {r["score"] for r in rows} == {95, 60}

    async def test_ticker_filter_is_case_insensitive(self, client: httpx.AsyncClient) -> None:
        assert len((await client.get("/api/feed?ticker=test")).json()) == 3

    async def test_unknown_ticker_is_empty_not_an_error(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/feed?ticker=NOPE")
        assert response.status_code == 200
        assert response.json() == []

    async def test_source_filter(self, client: httpx.AsyncClient) -> None:
        assert len((await client.get("/api/feed?source=edgar_8k")).json()) == 3
        assert (await client.get("/api/feed?source=halts")).json() == []

    async def test_filters_compose(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed?min_score=60&ticker=TEST&source=edgar_8k")).json()
        assert len(rows) == 2

    async def test_limit_is_respected(self, client: httpx.AsyncClient) -> None:
        assert len((await client.get("/api/feed?limit=1")).json()) == 1

    async def test_rejects_an_out_of_range_score(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/feed?min_score=500")).status_code == 422

    async def test_row_carries_what_the_ui_needs(self, client: httpx.AsyncClient) -> None:
        row = (await client.get("/api/feed?limit=1")).json()[0]
        assert row["ticker"] == "TEST"
        assert row["tier"] == "critical"
        assert row["score_parts"] == {"base": 95}
        # The list view must not ship the whole raw filing to every client.
        assert row["payload"] is None


class TestEvent:
    async def test_includes_the_raw_payload(self, client: httpx.AsyncClient) -> None:
        listed = (await client.get("/api/feed?limit=1")).json()[0]
        row = (await client.get(f"/api/event/{listed['id']}")).json()
        assert row["payload"] is not None

    async def test_missing_event_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/event/999999")).status_code == 404


class TestWatchlist:
    async def test_lists_watched_companies(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/watchlist")).json()
        assert [r["ticker"] for r in rows] == ["TEST"]

    async def test_counts_agree_with_the_feed(self, client: httpx.AsyncClient) -> None:
        """The inconsistency that showed up in the browser: the rail claimed a
        flag the feed would not return."""
        rail = (await client.get("/api/watchlist?min_score=30")).json()[0]
        feed = (await client.get("/api/feed?min_score=30&ticker=TEST")).json()
        assert rail["flags"] == len(feed)

    async def test_reports_the_top_score(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/watchlist?min_score=30")).json()[0]["top_score"] == 95


class TestStats:
    async def test_reports_threshold_and_unresolved(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/stats")).json()
        assert body["flag_threshold"] == 30
        assert body["unresolved"] == 0

    async def test_unmarked_events_do_not_appear_in_latency(
        self, client: httpx.AsyncClient
    ) -> None:
        """The seeded rows carry no live_capture marker, so they are exactly the
        case latency must ignore: their timestamps say nothing about how fast the
        pipeline notices a filing."""
        body = (await client.get("/api/stats")).json()
        assert body["latency"] == []
        assert body["excluded_from_latency"] == 3

    async def test_volume_reports_what_each_source_produced(
        self, client: httpx.AsyncClient
    ) -> None:
        """The panel's "is it alive" column. Latency can be empty while volume is
        not: these rows are excluded from latency but they were still produced."""
        body = (await client.get("/api/stats")).json()
        rows = {r["source"]: r for r in body["volume"]}
        assert rows["edgar_8k"]["recent"] == 3
        assert body["volume_window_days"] > 0

    async def test_a_source_with_no_history_is_not_called_quiet(
        self, client: httpx.AsyncClient
    ) -> None:
        """Absence only means something against a rate. A source that has never
        produced twelve a week says nothing by producing none this week -- halts go
        genuinely quiet for a fortnight, and calling that broken is a false alarm."""
        body = (await client.get("/api/stats")).json()
        assert [r["source"] for r in body["volume"] if r["quiet"]] == []


class TestLegend:
    """The dashboard's legend is served from /api/meta, beside the labels it
    explains -- a legend written in the frontend drifts from the vocabulary."""

    async def test_every_source_says_what_it_is(self, client: httpx.AsyncClient) -> None:
        """A new adapter cannot reach the filter bar without explaining itself."""
        sources = (await client.get("/api/meta")).json()["sources"]
        assert sources, "the filter bar has no sources to offer"
        assert [s["value"] for s in sources if not (s.get("hint") or "").strip()] == []

    async def test_a_hint_explains_rather_than_repeats_the_label(
        self, client: httpx.AsyncClient
    ) -> None:
        sources = {s["value"]: s for s in (await client.get("/api/meta")).json()["sources"]}
        hint = sources["edgar_144"]["hint"]
        assert hint != sources["edgar_144"]["label"]
        # The one thing about a 144 that is easy to get backwards.
        assert "intent" in hint.lower()

    async def test_hints_are_prose_not_source_comments(self, client: httpx.AsyncClient) -> None:
        """This codebase writes "--" for a dash in comments and docstrings. A hint
        is read on screen, where it renders as two hyphens and looks like a typo."""
        body = (await client.get("/api/meta")).json()
        hints = [s["hint"] for s in body["sources"]]
        hints += [c["hint"] for c in body["categories"] if c["hint"]]
        assert [h for h in hints if "--" in h] == []

    async def test_prose_uses_no_em_dashes(self, client: httpx.AsyncClient) -> None:
        """A house rule for everything that renders: no em dash as punctuation.

        Commas, colons and semicolons do the same work, and the vocabulary served
        from here is the prose most likely to acquire one, since it is written in
        Python and read on a web page.
        """
        body = (await client.get("/api/meta")).json()
        prose = [s["hint"] for s in body["sources"]]
        prose += [c["hint"] for c in body["categories"] if c["hint"]]
        prose += [s["label"] for s in body["sources"]]
        prose += [c["label"] for c in body["categories"]]
        assert [p for p in prose if "\u2014" in p] == []

    async def test_categories_are_hinted_only_where_the_label_is_not_enough(
        self, client: httpx.AsyncClient
    ) -> None:
        """Glossing "Insider buy" would bury the entries that need it."""
        categories = {
            c["value"]: c.get("hint") for c in (await client.get("/api/meta")).json()["categories"]
        }
        assert categories["sale_notice"]
        assert categories["insider_buy"] is None


class TestFlagCount:
    """The header reads "N flags · M routine" off these two counts, so a flag has
    to mean one thing at every filter setting."""

    async def test_flags_are_counted_at_the_threshold_not_the_slider(
        self, client: httpx.AsyncClient
    ) -> None:
        """Seeded: 95, 60, 20, with the threshold at 30. Showing everything must
        not turn the 20 into a flag -- it used to, because the count was anchored
        to max(min_score, 1) and reserved "routine" for scores of exactly zero."""
        response = await client.get("/api/feed?min_score=0")
        assert int(response.headers["X-Total-Count"]) == 3
        assert int(response.headers["X-Flag-Count"]) == 2

    async def test_a_narrowed_view_counts_only_what_it_shows(
        self, client: httpx.AsyncClient
    ) -> None:
        """Above the threshold the slider wins, so "flags in view" is the truth."""
        response = await client.get("/api/feed?min_score=90")
        assert int(response.headers["X-Total-Count"]) == 1
        assert int(response.headers["X-Flag-Count"]) == 1


class TestHealth:
    async def test_healthz(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/healthz")).json() == {"status": "ok"}


class TestTiers:
    @pytest.mark.parametrize(
        ("score", "tier"),
        [
            (100, "critical"),
            (85, "critical"),
            (84, "high"),
            (60, "high"),
            (59, "background"),
            (30, "background"),
            (29, "quiet"),
            (0, "quiet"),
        ],
    )
    def test_boundaries(self, score: int, tier: str) -> None:
        assert tier_for(score) == tier


class TestStatsExcludesBackfill:
    """A dashboard must not report a latency it knows to be wrong.

    The opening poll of a 100-entry window returns filings accepted hours
    earlier; replayed fixtures carry their original acceptance times. Both would
    measure the age of the backlog rather than how fast the pipeline notices.
    """

    async def _insert(self, pg_dsn: str, external_id: str, live: str | None, age_s: int) -> None:
        import asyncpg

        conn = await asyncpg.connect(pg_dsn)
        try:
            payload = {} if live is None else {"live_capture": live == "true"}
            await conn.execute(
                """
                insert into event (source, event_type, occurred_at, external_id,
                                   summary, score, payload)
                values ('edgar_8k','8k', now() - make_interval(secs => $2), $1, 'x', 50, $3::jsonb)
                """,
                external_id,
                age_s,
                json.dumps(payload),
            )
        finally:
            await conn.close()

    async def test_replayed_events_are_excluded(
        self, client: httpx.AsyncClient, pg_dsn: str
    ) -> None:
        await self._insert(pg_dsn, "replayed-1", None, 7200)
        body = (await client.get("/api/stats")).json()
        assert body["latency"] == []
        assert body["excluded_from_latency"] >= 1

    async def test_backfilled_events_are_excluded(
        self, client: httpx.AsyncClient, pg_dsn: str
    ) -> None:
        await self._insert(pg_dsn, "backfill-1", "false", 7200)
        body = (await client.get("/api/stats")).json()
        assert body["latency"] == []

    async def test_live_events_are_counted(self, client: httpx.AsyncClient, pg_dsn: str) -> None:
        await self._insert(pg_dsn, "live-1", "true", 30)
        body = (await client.get("/api/stats")).json()
        assert len(body["latency"]) == 1
        assert body["latency"][0]["events"] == 1
        assert 25 <= body["latency"][0]["p50_seconds"] <= 40

    async def test_a_backfill_cannot_drag_the_median(
        self, client: httpx.AsyncClient, pg_dsn: str
    ) -> None:
        """The specific failure: one two-hour-old backfilled filing next to a
        live one would otherwise report a p50 in the thousands of seconds."""
        await self._insert(pg_dsn, "live-2", "true", 30)
        await self._insert(pg_dsn, "backfill-2", "false", 7200)
        body = (await client.get("/api/stats")).json()
        assert body["latency"][0]["p50_seconds"] < 60

    async def test_the_response_says_what_it_excluded(
        self, client: httpx.AsyncClient, pg_dsn: str
    ) -> None:
        """Visibly a subset, rather than silently one."""
        await self._insert(pg_dsn, "replayed-2", None, 7200)
        body = (await client.get("/api/stats")).json()
        assert body["excluded_from_latency"] >= 1
        assert "worker started" in body["latency_note"]


class TestUniverses:
    """Which monitor you are looking through."""

    @staticmethod
    async def _members(pg: asyncpg.Connection) -> None:
        core_id = await pg.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000011', 'CORE', 'Core Inc.', true) returning id"
        )
        sp_id = await pg.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000012', 'SPX', 'Spx Inc.', true) returning id"
        )
        await pg.execute(
            "insert into universe_member (company_id, universe) values ($1,'core'), ($2,'sp500')",
            core_id,
            sp_id,
        )
        for company_id, external_id, category in (
            (core_id, "u1", "other"),
            (sp_id, "u2", "other"),
            (sp_id, "u3", "earnings"),
        ):
            await pg.execute(
                """
                insert into event (company_id, source, event_type, occurred_at, external_id,
                                   score, payload, category)
                values ($1, 'edgar_8k', '8k', $2, $3, 55, '{}'::jsonb, $4)
                """,
                company_id,
                NOW,
                external_id,
                category,
            )

    async def test_feed_filters_by_universe(
        self, client: httpx.AsyncClient, pg: asyncpg.Connection
    ) -> None:
        await self._members(pg)
        core = await client.get("/api/feed?universe=core&min_score=0")
        sp500 = await client.get("/api/feed?universe=sp500&min_score=0")
        everything = await client.get("/api/feed?universe=all&min_score=0")

        assert [e["ticker"] for e in core.json()] == ["CORE"]
        assert {e["ticker"] for e in sp500.json()} == {"SPX"}
        assert {e["ticker"] for e in everything.json()} >= {"CORE", "SPX"}
        assert core.headers["X-Total-Count"] == "1"

    async def test_rejects_an_unknown_universe(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/feed?universe=nasdaq100")
        assert response.status_code == 422
        assert "universe" in response.json()["detail"]

    async def test_exclude_category_hides_only_that_kind(
        self, client: httpx.AsyncClient, pg: asyncpg.Connection
    ) -> None:
        """What the index view needs by default: every member reports earnings
        once a quarter, which is news you already expected."""
        await self._members(pg)
        response = await client.get(
            "/api/feed?universe=sp500&min_score=0&exclude_category=earnings"
        )
        assert [e["category"] for e in response.json()] == ["other"]

    async def test_rejects_an_unknown_excluded_category(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/feed?exclude_category=nonsense")
        assert response.status_code == 422

    async def test_the_rail_can_be_asked_for_one_universe(
        self, client: httpx.AsyncClient, pg: asyncpg.Connection
    ) -> None:
        await self._members(pg)
        rows = (await client.get("/api/watchlist?min_score=1&universe=core")).json()
        assert [r["ticker"] for r in rows] == ["CORE"]


class TestActive:
    async def test_lists_the_busiest_names_in_a_universe(
        self, client: httpx.AsyncClient, pg: asyncpg.Connection
    ) -> None:
        busy_id = await pg.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000013', 'BUSY', 'Busy Inc.', true) returning id"
        )
        quiet_id = await pg.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000014', 'QUIET', 'Quiet Inc.', true) returning id"
        )
        await pg.execute(
            "insert into universe_member (company_id, universe) values ($1,'sp500'), ($2,'sp500')",
            busy_id,
            quiet_id,
        )
        for n, score in enumerate((60, 70, 40)):
            await pg.execute(
                """
                insert into event (company_id, source, event_type, occurred_at, external_id,
                                   score, payload, category)
                values ($1, 'edgar_8k', '8k', $2, $3, $4, '{}'::jsonb, 'other')
                """,
                busy_id,
                NOW,
                f"busy{n}",
                score,
            )

        rows = (await client.get("/api/active?universe=sp500&min_score=30&limit=10")).json()
        assert [r["ticker"] for r in rows] == ["BUSY"], "a company with no flags is not active"
        assert rows[0]["flags"] == 3
        assert rows[0]["top_score"] == 70

    async def test_rejects_all_as_a_universe(self, client: httpx.AsyncClient) -> None:
        """ "all" is the absence of a filter, which is not a rail."""
        assert (await client.get("/api/active?universe=all")).status_code == 422


class TestForm144Vocabulary:
    """The filter bar is built from /api/meta, so a new source has to appear there
    or it is unreachable in the UI."""

    async def test_form_144_is_offered_as_a_source(self, client: httpx.AsyncClient) -> None:
        sources = (await client.get("/api/meta")).json()["sources"]
        labels = {s["value"]: s["label"] for s in sources}
        assert labels["edgar_144"] == "Form 144"

    async def test_planned_sale_is_offered_as_a_category(self, client: httpx.AsyncClient) -> None:
        categories = (await client.get("/api/meta")).json()["categories"]
        labels = {c["value"]: c["label"] for c in categories}
        assert labels["sale_notice"] == "Planned sale (144)"

    async def test_a_notice_can_be_filtered_by_source(
        self, client: httpx.AsyncClient, pg: asyncpg.Connection
    ) -> None:
        company_id = await pg.fetchval(
            "insert into company (cik, ticker, name, watched) "
            "values ('0000000041', 'NOTE', 'Note Inc.', true) returning id"
        )
        await pg.execute(
            """
            insert into event (company_id, source, event_type, occurred_at, external_id,
                               score, payload, category)
            values ($1, 'edgar_144', 'sale_notice', $2, 'n1', 45,
                    '{"headline": "Planned sale by A Seller · $1,000,000"}'::jsonb, 'sale_notice')
            """,
            company_id,
            NOW,
        )
        rows = (await client.get("/api/feed?source=edgar_144&min_score=0")).json()
        assert [r["headline"] for r in rows] == ["Planned sale by A Seller · $1,000,000"]
        assert rows[0]["category"] == "sale_notice"
