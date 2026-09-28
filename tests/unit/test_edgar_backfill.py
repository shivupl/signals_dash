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


class TestSweepSlicing:
    """524 companies in one sweep is a ~500-request burst every thirty minutes.
    Core names are reconciled every sweep; the rest rotate, covering the index
    every four sweeps -- two hours, far inside the three-day lookback."""

    @staticmethod
    def _ctx(*, core: bool = True) -> FetchContext:
        return FetchContext(
            http=SourceClient(
                UA,
                clock=FakeClock(),
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{}")),
            ),
            clock=FakeClock(),
            watched_ciks=frozenset(f"{n:010d}" for n in range(1, 13)),
            core_ciks=frozenset(f"{n:010d}" for n in range(1, 5)) if core else frozenset(),
            state={},
        )

    def test_core_is_swept_every_time(self) -> None:
        adapter = EdgarBackfillAdapter(slices=4)
        ctx = self._ctx()
        core = {f"{n:010d}" for n in range(1, 5)}
        for _ in range(4):
            assert core <= set(adapter.sweep_ciks(ctx))

    def test_the_rest_is_fully_covered_in_one_rotation(self) -> None:
        adapter = EdgarBackfillAdapter(slices=4)
        ctx = self._ctx()
        seen: set[str] = set()
        for _ in range(4):
            seen |= set(adapter.sweep_ciks(ctx))
        assert seen == ctx.watched_ciks

    def test_one_sweep_is_a_fraction_of_the_universe(self) -> None:
        adapter = EdgarBackfillAdapter(slices=4)
        assert len(adapter.sweep_ciks(self._ctx())) == 6, "4 core plus 2 of the 8 others"

    def test_no_core_set_means_sweep_everything(self) -> None:
        """With --all, or before universes are seeded, behave exactly as before."""
        adapter = EdgarBackfillAdapter(slices=4)
        ctx = self._ctx(core=False)
        assert set(adapter.sweep_ciks(ctx)) == ctx.watched_ciks

    def test_a_cik_watched_but_not_in_any_universe_is_still_swept(self) -> None:
        """`--all` widens watched_ciks without touching membership; nothing
        watched may fall out of reconciliation."""
        adapter = EdgarBackfillAdapter(slices=1)
        ctx = self._ctx()
        assert set(adapter.sweep_ciks(ctx)) == ctx.watched_ciks


class TestForm144ThroughTheSweep:
    """The regression this guards: adding 144 to the swept forms without adding its
    hydration meant the sweep handed a doc-less payload to the Form 4 normalizer and
    raised KeyError -- so reconciliation broke for every watched company that filed
    one, which is silent until it happens."""

    @staticmethod
    def _rig(submission: bytes):
        from signals.parsers.form144_xml import parse_form144  # noqa: F401 -- import guard

        notice = read_fixture("edgar", "form144_officer_ns2.txt")
        body = submissions(
            [("144", "0001958244-26-000624", "2026-09-25T19:19:45.000Z", "")]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "submissions" in url:
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=submission or notice)

        clock = FakeClock(datetime(2026, 9, 26, 12, 0, tzinfo=UTC))
        ctx = FetchContext(
            http=SourceClient(UA, clock=clock, transport=httpx.MockTransport(handler)),
            clock=clock,
            watched_ciks=frozenset({"0000000042"}),
            state={},
        )
        return EdgarBackfillAdapter(), ctx

    async def test_a_notice_is_hydrated_and_normalized(self) -> None:
        adapter, ctx = self._rig(read_fixture("edgar", "form144_officer_ns2.txt"))
        raws = await adapter.fetch(ctx)
        assert len(raws) == 1
        assert "doc" in raws[0].payload, "a 144 must arrive hydrated"
        events = adapter.normalize(raws[0])
        assert [e.source for e in events] == ["edgar_144"]
        assert events[0].payload["value"] == 56_900_000.0

    async def test_an_unparseable_notice_is_skipped_not_raised(self) -> None:
        adapter, ctx = self._rig(b"<html>maintenance</html>")
        assert await adapter.fetch(ctx) == []


class TestShards:
    """`filings.recent` caps at 1,000 filings. For most companies that reaches back
    years; for a heavy filer it can run out inside months, and the history then just
    stops without saying so."""

    @staticmethod
    def _body(files: list[dict[str, str]]) -> bytes:
        return json.dumps(
            {
                "cik": "42",
                "name": "Watched Corp",
                "filings": {"recent": {"form": [], "accessionNumber": [],
                                        "acceptanceDateTime": [], "items": []},
                            "files": files},
            }
        ).encode()

    def test_a_shard_overlapping_the_window_is_wanted(self) -> None:
        from signals.adapters.edgar_backfill import shard_names

        body = self._body([{"name": "CIK42-submissions-001.json", "filingTo": "2025-06-30"}])
        assert shard_names(body, datetime(2025, 1, 1, tzinfo=UTC)) == [
            "CIK42-submissions-001.json"
        ]

    def test_a_shard_entirely_older_than_the_window_is_skipped(self) -> None:
        from signals.adapters.edgar_backfill import shard_names

        body = self._body([{"name": "CIK42-submissions-001.json", "filingTo": "2015-07-26"}])
        assert shard_names(body, datetime(2025, 1, 1, tzinfo=UTC)) == []

    def test_a_shard_straddling_the_cutoff_is_kept(self) -> None:
        """filingTo is a filing date, not an acceptance time; a shard ending on the
        cutoff day can still hold filings accepted after it."""
        from signals.adapters.edgar_backfill import shard_names

        body = self._body([{"name": "CIK42-submissions-001.json", "filingTo": "2025-01-01"}])
        assert shard_names(body, datetime(2025, 1, 1, 18, tzinfo=UTC)) != []

    def test_no_shards_is_the_common_case(self) -> None:
        from signals.adapters.edgar_backfill import shard_names

        assert shard_names(self._body([]), datetime(2025, 1, 1, tzinfo=UTC)) == []

    def test_a_shard_is_parsed_without_the_filings_wrapper(self) -> None:
        """Shard documents are flat: no "filings" key, and no company identity of
        their own, so the caller supplies it."""
        from signals.adapters.edgar_backfill import shard_filings

        flat = json.dumps(
            {
                "form": ["8-K", "10-K"],
                "accessionNumber": ["0000000000-25-000001", "0000000000-25-000002"],
                "acceptanceDateTime": ["2025-06-01T12:00:00.000Z", "2025-06-02T12:00:00.000Z"],
                "items": ["5.02", ""],
            }
        ).encode()
        got = shard_filings(
            flat, datetime(2025, 1, 1, tzinfo=UTC), cik="0000000042", name="Watched Corp"
        )
        assert [f["form"] for f in got] == ["8-K"], "10-K is not a watched form"
        assert got[0]["cik"] == "0000000042"
        assert got[0]["name"] == "Watched Corp"
        assert got[0]["items"] == ["5.02"]
