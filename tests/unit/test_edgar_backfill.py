"""Reconciliation against SEC's per-company records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from signals.adapters.base import FetchContext
from signals.adapters.edgar_backfill import EdgarBackfillAdapter, as_group, recent_filings
from signals.http import SourceClient
from signals.parsers.edgar_titles import Role
from signals.pipeline.watchdog import stale_after
from tests.conftest import read_fixture
from tests.fakes import FakeClock

APPLE = read_fixture("edgar", "submissions_CIK0000320193.json")
PURCHASE = read_fixture("edgar", "form4_purchase_P.txt")
UA = "Signals/0.1 (test@example.com)"
LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


def submissions(rows: list[tuple[str, str, str, str]]) -> bytes:
    forms, accs, accepted, items = zip(*rows, strict=True) if rows else ([], [], [], [])
    return json.dumps(
        {
            "cik": "42",
            "name": "Watched Corp",
            "filings": {
                "recent": {
                    "form": list(forms),
                    "accessionNumber": list(accs),
                    "acceptanceDateTime": list(accepted),
                    "items": list(items),
                }
            },
        }
    ).encode()


NOW = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
ROWS = [
    ("8-K", "0000000042-26-000001", "2026-09-18T19:00:00.000Z", "5.02,9.01"),
    ("4", "0000000042-26-000002", "2026-09-18T18:00:00.000Z", ""),
    ("10-Q", "0000000042-26-000003", "2026-09-18T17:00:00.000Z", ""),
    ("8-K", "0000000042-26-000004", "2026-09-01T17:00:00.000Z", "2.02"),
]


class TestRecentFilings:
    def test_parses_a_real_submissions_document(self) -> None:
        filings = recent_filings(APPLE, LONG_AGO)
        assert filings
        assert {f["cik"] for f in filings} == {"0000320193"}
        assert all(f["accepted"].tzinfo is not None for f in filings)

    def test_keeps_only_the_forms_the_feed_covers(self) -> None:
        forms = {f["form"] for f in recent_filings(submissions(ROWS), LONG_AGO)}
        assert forms == {"8-K", "4"}

    def test_respects_the_lookback(self) -> None:
        got = recent_filings(submissions(ROWS), NOW - timedelta(days=3))
        assert [f["accession"][-1] for f in got] == ["1", "2"]

    def test_acceptance_time_is_utc(self) -> None:
        """The records say Z. Reading it as Eastern would shift every backfilled
        event four hours and scramble the feed's ordering."""
        filing = recent_filings(submissions(ROWS), LONG_AGO)[0]
        assert filing["accepted"] == datetime(2026, 9, 18, 19, 0, tzinfo=UTC)

    def test_items_are_split_for_the_8k_scorer(self) -> None:
        assert recent_filings(submissions(ROWS), LONG_AGO)[0]["items"] == ["5.02", "9.01"]


class TestAsGroup:
    def test_a_form4_company_is_the_issuer(self) -> None:
        group = as_group(recent_filings(submissions(ROWS), LONG_AGO)[1])
        assert group.entries[0].role is Role.ISSUER
        assert group.issuer_hint == "0000000042"

    def test_the_link_points_at_the_filing_index(self) -> None:
        group = as_group(recent_filings(submissions(ROWS), LONG_AGO)[0])
        assert group.link == (
            "https://www.sec.gov/Archives/edgar/data/42/000000004226000001/"
            "0000000042-26-000001-index.htm"
        )


def make_ctx(clock: FakeClock, log: list[str]) -> FetchContext:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        log.append(url)
        if "submissions" in url:
            return httpx.Response(200, content=submissions(ROWS))
        return httpx.Response(200, content=PURCHASE)

    return FetchContext(
        http=SourceClient(UA, transport=httpx.MockTransport(handler), clock=clock),
        clock=clock,
        watched_ciks=frozenset({"0000000042"}),
    )


class TestSweep:
    async def test_finds_what_the_index_poll_could_have_missed(self) -> None:
        clock, log = FakeClock(NOW), []
        adapter = EdgarBackfillAdapter()
        raws = await adapter.fetch(make_ctx(clock, log))
        assert {r.external_id[-1] for r in raws} == {"1", "2"}

    async def test_events_are_indistinguishable_from_the_index_path(self) -> None:
        """Same source names and the accession as the id, so whatever the index
        already caught is a dedupe no-op rather than a second row."""
        clock, log = FakeClock(NOW), []
        adapter = EdgarBackfillAdapter()
        raws = await adapter.fetch(make_ctx(clock, log))
        events = [e for r in raws for e in adapter.normalize(r)]
        by_source = {e.source: e for e in events}
        assert set(by_source) == {"edgar_8k", "edgar_form4"}
        assert by_source["edgar_8k"].external_id == "0000000042-26-000001"
        assert by_source["edgar_8k"].payload["items"] == ["5.02", "9.01"]
        assert by_source["edgar_form4"].event_type == "form4_buy"

    async def test_a_second_sweep_refetches_no_documents(self) -> None:
        clock, log = FakeClock(NOW), []
        adapter, ctx = EdgarBackfillAdapter(), make_ctx(clock, log)
        await adapter.fetch(ctx)
        documents = len([u for u in log if u.endswith(".txt")])
        assert await adapter.fetch(ctx) == []
        assert len([u for u in log if u.endswith(".txt")]) == documents

    async def test_one_company_failing_does_not_stop_the_sweep(self) -> None:
        clock = FakeClock(NOW)

        def handler(request: httpx.Request) -> httpx.Response:
            if "CIK0000000001" in str(request.url):
                return httpx.Response(500)
            if "submissions" in str(request.url):
                return httpx.Response(200, content=submissions(ROWS[:1]))
            return httpx.Response(200, content=PURCHASE)

        ctx = FetchContext(
            http=SourceClient(UA, transport=httpx.MockTransport(handler), clock=clock),
            clock=clock,
            watched_ciks=frozenset({"0000000001", "0000000042"}),
        )
        assert len(await EdgarBackfillAdapter().fetch(ctx)) == 1

    async def test_a_form4_that_fails_to_hydrate_is_retried_next_sweep(self) -> None:
        clock = FakeClock(NOW)
        state = {"fail": True}

        def handler(request: httpx.Request) -> httpx.Response:
            if "submissions" in str(request.url):
                return httpx.Response(200, content=submissions(ROWS[1:2]))
            return httpx.Response(500) if state["fail"] else httpx.Response(200, content=PURCHASE)

        ctx = FetchContext(
            http=SourceClient(UA, transport=httpx.MockTransport(handler), clock=clock),
            clock=clock,
            watched_ciks=frozenset({"0000000042"}),
        )
        adapter = EdgarBackfillAdapter()
        assert await adapter.fetch(ctx) == []
        state["fail"] = False
        ctx.http.limiter_for("www.sec.gov").reset()
        assert len(await adapter.fetch(ctx)) == 1


class TestWatchdogScaling:
    def test_fast_adapters_keep_the_thirty_minute_alarm(self) -> None:
        assert stale_after(2.0) == 30.0
        assert stale_after(30.0) == 30.0

    def test_a_half_hourly_sweep_does_not_alarm_on_schedule(self) -> None:
        """It would otherwise trip a thirty-minute alarm every time it ran on time."""
        assert stale_after(1800.0) == 75.0
