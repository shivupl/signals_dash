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


class TestHealth:
    async def test_healthz(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/healthz")).json() == {"status": "ok"}


class TestTiers:
    @pytest.mark.parametrize(
        ("score", "tier"),
        [(100, "critical"), (85, "critical"), (84, "high"), (60, "high"),
         (59, "background"), (30, "background"), (29, "quiet"), (0, "quiet")],
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

    async def test_live_events_are_counted(
        self, client: httpx.AsyncClient, pg_dsn: str
    ) -> None:
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
