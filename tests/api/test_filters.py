"""Feed filters, counts, the system strip and settings."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from pytest_socket import enable_socket

from signals.api import deps
from signals.api.app import create_app
from signals.api.routes_feed import split, window
from signals.config import Settings
from signals.store.pg import PgStore

pytestmark = pytest.mark.pg
NOW = datetime.now(tz=UTC)


async def seed(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        ids = {}
        for cik, tk in [("0000000001", "AAA"), ("0000000002", "BBB")]:
            ids[tk] = await conn.fetchval(
                "insert into company (cik,ticker,name,watched) values ($1,$2,$3,true) returning id",
                cik,
                tk,
                f"{tk} Corp",
            )
        rows = [
            ("AAA", "edgar_8k", "8k", 95, "distress", 1, {"items": ["4.02"]}),
            ("AAA", "edgar_form4", "form4_buy", 65, "insider_buy", 2, {"codes": ["P"]}),
            ("AAA", "edgar_form4", "form4_other", 0, "insider_sell", 3, {"codes": ["S"]}),
            ("BBB", "edgar_8k", "8k", 45, "officer_change", 4, {"items": ["5.02"]}),
            ("BBB", "halts", "halt", 90, "halt", 40 * 24, {"reason": "T1"}),
            (
                None,
                "system",
                "source_stale",
                70,
                "system",
                1,
                {"state": "open", "adapter": "halts", "headline": "halts degraded 55m"},
            ),
            (
                None,
                "system",
                "source_recovered",
                30,
                "system",
                2,
                {"state": "info", "adapter": "edgar_8k", "headline": "edgar_8k recovered"},
            ),
        ]
        for i, (tk, source, etype, score, cat, hours_ago, payload) in enumerate(rows):
            await conn.execute(
                """insert into event (company_id, source, event_type, occurred_at, external_id,
                                      summary, score, payload, category)
                   values ($1,$2,$3,$4,$5,'s',$6,$7::jsonb,$8)""",
                ids.get(tk),
                source,
                etype,
                NOW - timedelta(hours=hours_ago),
                f"e{i}",
                score,
                json.dumps(payload),
                cat,
            )
    finally:
        await conn.close()


@pytest.fixture
async def client(pg_dsn: str, monkeypatch: pytest.MonkeyPatch):
    enable_socket()
    await seed(pg_dsn)
    store = await PgStore.connect(pg_dsn)
    deps.set_store(store)
    deps.set_settings(
        Settings.from_env(
            {"DATABASE_URL": pg_dsn, "FLAG_THRESHOLD": "30", "ADMIN_TOKEN": "s3cret"},
            require_sec_user_agent=False,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as c:
        yield c
    await store.close()
    deps.set_store(None)


class TestSystemStaysOutOfTheFeed:
    async def test_excluded_by_default(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed")).json()
        assert all(r["source"] != "system" for r in rows)
        assert len(rows) == 5

    async def test_included_on_request(self, client: httpx.AsyncClient) -> None:
        assert len((await client.get("/api/feed?system=true")).json()) == 7

    async def test_asking_for_the_system_source_is_asking_to_see_it(
        self, client: httpx.AsyncClient
    ) -> None:
        rows = (await client.get("/api/feed?source=system")).json()
        assert {r["source"] for r in rows} == {"system"}

    async def test_the_strip_lists_open_outages_first(self, client: httpx.AsyncClient) -> None:
        strip = (await client.get("/api/system")).json()
        assert [s["state"] for s in strip] == ["open", "info"]
        assert strip[0]["headline"] == "halts degraded 55m"


class TestCounts:
    async def test_total_matches_the_rows(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/feed?min_score=30")
        assert int(r.headers["X-Total-Count"]) == len(r.json()) == 4

    async def test_a_zero_score_row_is_an_event_but_not_a_flag(
        self, client: httpx.AsyncClient
    ) -> None:
        r = await client.get("/api/feed?min_score=0")
        assert int(r.headers["X-Total-Count"]) == 5
        assert int(r.headers["X-Flag-Count"]) == 4

    async def test_system_events_never_count_as_flags(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/feed?min_score=0&system=true")
        assert int(r.headers["X-Total-Count"]) == 7
        assert int(r.headers["X-Flag-Count"]) == 4

    async def test_the_header_and_the_rail_agree(self, client: httpx.AsyncClient) -> None:
        """Same threshold, same window, same answer."""
        feed = await client.get("/api/feed?min_score=30&range=7d")
        rail = (await client.get("/api/watchlist?min_score=30")).json()
        assert int(feed.headers["X-Flag-Count"]) == sum(r["flags"] for r in rail)

    async def test_the_count_ignores_the_limit(self, client: httpx.AsyncClient) -> None:
        r = await client.get("/api/feed?limit=1")
        assert len(r.json()) == 1
        assert int(r.headers["X-Total-Count"]) == 5


class TestMultiSelect:
    async def test_tickers(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed?ticker=aaa,BBB")).json()
        assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
        assert {r["ticker"] for r in (await client.get("/api/feed?ticker=BBB")).json()} == {"BBB"}

    async def test_sources(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed?source=edgar_form4,halts")).json()
        assert {r["source"] for r in rows} == {"edgar_form4", "halts"}

    async def test_categories(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed?category=insider_buy,insider_sell")).json()
        assert {r["category"] for r in rows} == {"insider_buy", "insider_sell"}

    async def test_an_unknown_category_is_rejected_not_silently_empty(
        self, client: httpx.AsyncClient
    ) -> None:
        assert (await client.get("/api/feed?category=nonsense")).status_code == 422

    async def test_filters_compose(self, client: httpx.AsyncClient) -> None:
        rows = (await client.get("/api/feed?ticker=AAA&category=distress&min_score=85")).json()
        assert len(rows) == 1


class TestDateRange:
    async def test_named_ranges(self, client: httpx.AsyncClient) -> None:
        assert len((await client.get("/api/feed?range=7d")).json()) == 4
        assert len((await client.get("/api/feed?range=30d")).json()) == 4
        assert len((await client.get("/api/feed")).json()) == 5

    async def test_custom_range(self, client: httpx.AsyncClient) -> None:
        since = (NOW - timedelta(days=45)).isoformat()
        until = (NOW - timedelta(days=35)).isoformat()
        rows = (await client.get("/api/feed", params={"since": since, "until": until})).json()
        assert [r["source"] for r in rows] == ["halts"]

    async def test_a_bad_range_is_rejected(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/feed?range=fortnight")).status_code == 422

    def test_today_means_the_markets_today(self) -> None:
        """23:30 ET on the 18th is already the 19th in UTC. It is still the 18th."""
        late = datetime(2026, 9, 19, 3, 30, tzinfo=UTC)
        since, _ = window("today", None, None, now=late)
        assert since == datetime(2026, 9, 18, 4, 0, tzinfo=UTC)

    def test_split(self) -> None:
        assert split("a, b,,c ") == ("a", "b", "c")
        assert split(None) == ()


class TestSettings:
    async def test_readable_by_anyone(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/settings")).json()
        assert body == {"flag_threshold": 30, "editable": True}

    async def test_changing_needs_the_token(self, client: httpx.AsyncClient) -> None:
        assert (await client.put("/api/settings", json={"flag_threshold": 50})).status_code == 401
        wrong = await client.put(
            "/api/settings", json={"flag_threshold": 50}, headers={"X-Admin-Token": "nope"}
        )
        assert wrong.status_code == 401

    async def test_a_change_sticks_and_shows_in_stats(self, client: httpx.AsyncClient) -> None:
        ok = await client.put(
            "/api/settings", json={"flag_threshold": 50}, headers={"X-Admin-Token": "s3cret"}
        )
        assert ok.json()["flag_threshold"] == 50
        assert (await client.get("/api/stats")).json()["flag_threshold"] == 50

    async def test_changing_the_threshold_deletes_nothing(self, client: httpx.AsyncClient) -> None:
        before = int((await client.get("/api/feed")).headers["X-Total-Count"])
        await client.put(
            "/api/settings", json={"flag_threshold": 99}, headers={"X-Admin-Token": "s3cret"}
        )
        after = (await client.get("/api/feed")).json()
        assert len(after) == before
        assert {r["score"] for r in after} == {0, 45, 65, 90, 95}

    async def test_out_of_range_is_rejected(self, client: httpx.AsyncClient) -> None:
        r = await client.put(
            "/api/settings", json={"flag_threshold": 500}, headers={"X-Admin-Token": "s3cret"}
        )
        assert r.status_code == 422

    async def test_without_a_configured_token_it_is_read_only(
        self, client: httpx.AsyncClient, pg_dsn: str
    ) -> None:
        deps.set_settings(
            Settings.from_env({"DATABASE_URL": pg_dsn}, require_sec_user_agent=False)
        )
        assert (await client.get("/api/settings")).json()["editable"] is False
        r = await client.put(
            "/api/settings", json={"flag_threshold": 50}, headers={"X-Admin-Token": "anything"}
        )
        assert r.status_code == 403


class TestMeta:
    async def test_the_filter_bar_vocabulary(self, client: httpx.AsyncClient) -> None:
        body = (await client.get("/api/meta")).json()
        assert "distress" in {c["value"] for c in body["categories"]}
        assert {s["value"] for s in body["sources"]} >= {"edgar_8k", "halts", "system"}
