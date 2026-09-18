"""The adapter loop: isolation, heartbeat, backoff, scheduling.

Every test here runs against a FakeClock, so simulated hours pass instantly and
the wait the runner chose is directly observable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from signals.clock import EASTERN, MarketCalendar
from signals.errors import PermanentSourceError, RateLimited, TransientSourceError
from signals.pipeline.runner import IDLE_INTERVAL, MAX_BACKOFF, AdapterRunner
from tests.fakes import FakeAdapter, FakeClock

CAL = MarketCalendar.load()

# A Tuesday, mid-session.
OPEN = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)  # 10:00 ET
AFTER_HOURS = datetime(2026, 9, 15, 23, 0, tzinfo=UTC)  # 19:00 ET
OVERNIGHT = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)  # 02:00 ET
WEEKEND = datetime(2026, 9, 19, 16, 0, tzinfo=UTC)  # Saturday


class _NullProcessor:
    def __init__(self) -> None:
        self.seen = 0

    async def process(self, event: object) -> object:
        from signals.pipeline.process import ProcessStats

        self.seen += 1
        return ProcessStats(seen=1)


def make_runner(adapter: FakeAdapter, clock: FakeClock) -> AdapterRunner:
    return AdapterRunner(
        adapter=adapter,
        processor=_NullProcessor(),  # type: ignore[arg-type]
        http=None,  # type: ignore[arg-type]
        clock=clock,
        calendar=CAL,
    )


class TestErrorIsolation:
    async def test_a_failure_does_not_stop_the_loop(self) -> None:
        """The single most important property: one bad iteration must not end
        the source."""
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[[], TransientSourceError("boom"), []])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=3)

        assert adapter.calls == 3
        assert runner.health.iterations == 2  # the failed one does not count

    async def test_an_unexpected_exception_is_contained(self) -> None:
        """A parser bug is not a Transient/Permanent error, and must still be
        caught -- otherwise one malformed filing kills the source for the day."""
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[ValueError("parser bug"), []])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=2)

        assert adapter.calls == 2
        assert runner.health.iterations == 1

    async def test_permanent_errors_also_keep_the_loop_alive(self) -> None:
        """A 403 means stop hammering, not stop existing -- the circuit breaker
        handles the pause, the loop stays up to recover."""
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[PermanentSourceError("403"), []])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=2)
        assert runner.health.iterations == 1

    async def test_one_adapter_failing_leaves_another_untouched(self) -> None:
        import asyncio

        clock = FakeClock(OPEN)
        broken = FakeAdapter("broken", script=[TransientSourceError("x")] * 3)
        healthy = FakeAdapter("healthy", script=[[], [], []])
        runners = [make_runner(broken, clock), make_runner(healthy, clock)]

        await asyncio.gather(*(r.run(max_iterations=3) for r in runners))

        assert runners[0].health.iterations == 0
        assert runners[1].health.iterations == 3


class TestHeartbeat:
    async def test_advances_only_on_success(self) -> None:
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[[], TransientSourceError("boom")])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)
        first = runner.health.last_success
        assert first is not None

        await runner.run(max_iterations=1)
        assert runner.health.last_success == first, "a failure moved the heartbeat"

    async def test_records_the_error_text(self) -> None:
        clock = FakeClock(OPEN)
        runner = make_runner(FakeAdapter(script=[TransientSourceError("edgar down")]), clock)
        await runner.run(max_iterations=1)
        assert runner.health.last_error is not None
        assert "edgar down" in runner.health.last_error

    async def test_success_clears_a_previous_error(self) -> None:
        clock = FakeClock(OPEN)
        runner = make_runner(FakeAdapter(script=[TransientSourceError("x"), []]), clock)
        await runner.run(max_iterations=2)
        assert runner.health.last_error is None
        assert runner.health.consecutive_failures == 0


class TestBackoff:
    async def test_grows_with_consecutive_failures(self) -> None:
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[TransientSourceError("x")] * 3, interval=1.0)
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=3)

        waits = clock.sleeps
        assert waits == sorted(waits), f"backoff did not grow: {waits}"
        assert waits[0] < waits[-1]

    async def test_is_capped(self) -> None:
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[TransientSourceError("x")] * 8, interval=2.0)
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=8)

        assert max(clock.sleeps) <= MAX_BACKOFF

    async def test_resets_after_a_success(self) -> None:
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(
            script=[
                TransientSourceError("x"),
                TransientSourceError("x"),
                [],
                TransientSourceError("x"),
            ],
            interval=1.0,
        )
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=4)

        assert clock.sleeps[-1] < clock.sleeps[1], "backoff did not reset after the success"

    async def test_rate_limit_honours_retry_after(self) -> None:
        """When the server names a wait, use it rather than our own guess."""
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[RateLimited("429", retry_after=42.0)], interval=1.0)
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)

        assert 42.0 in clock.sleeps


class TestFirstFailureIsFree:
    async def test_a_single_miss_retries_at_the_normal_interval(self) -> None:
        """One-off failures are routine -- a stale connection, a CDN challenge --
        and backing off after one just widens the hole in coverage."""
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[TransientSourceError("blip")], interval=2.0)
        runner = make_runner(adapter, clock)
        await runner.run(max_iterations=1)
        assert clock.sleeps == [2.0]

    async def test_the_multiplier_starts_at_the_second_consecutive_failure(self) -> None:
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[TransientSourceError("x")] * 3, interval=2.0)
        runner = make_runner(adapter, clock)
        await runner.run(max_iterations=3)
        assert clock.sleeps == [2.0, 8.0, 32.0]


class TestScheduling:
    async def test_paces_itself_to_the_interval(self) -> None:
        clock = FakeClock(OPEN)
        adapter = FakeAdapter(script=[[], []], interval=2.0)
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=2)

        assert clock.sleeps == [2.0, 2.0]

    async def test_runs_after_the_close(self) -> None:
        """Filings do not stop at 16:00 -- the heaviest 8-K window is just after
        it. An adapter that slept at the bell would miss the best material."""
        clock = FakeClock(AFTER_HOURS)
        adapter = FakeAdapter(script=[[]])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)

        assert adapter.calls == 1

    async def test_idles_overnight(self) -> None:
        clock = FakeClock(OVERNIGHT)
        adapter = FakeAdapter(script=[[]])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)

        assert adapter.calls == 0
        assert clock.sleeps == [IDLE_INTERVAL]

    async def test_idles_at_the_weekend(self) -> None:
        clock = FakeClock(WEEKEND)
        adapter = FakeAdapter(script=[[]])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)

        assert adapter.calls == 0

    async def test_market_hours_only_adapter_sleeps_after_the_close(self) -> None:
        clock = FakeClock(AFTER_HOURS)
        adapter = FakeAdapter(script=[[]])
        adapter.market_hours_only = True  # type: ignore[misc]
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)

        assert adapter.calls == 0

    @pytest.mark.parametrize(
        ("hour_et", "should_run"),
        [(5, False), (6, True), (9, True), (16, True), (21, True), (22, False), (23, False)],
    )
    async def test_the_ingest_window(self, hour_et: int, should_run: bool) -> None:
        clock = FakeClock(datetime(2026, 9, 15, hour_et, 30, tzinfo=EASTERN).astimezone(UTC))
        adapter = FakeAdapter(script=[[]])
        runner = make_runner(adapter, clock)

        await runner.run(max_iterations=1)

        assert (adapter.calls == 1) is should_run


class TestDiscoveryForensics:
    """A late capture has two possible causes, and one number tells them apart:
    a poll gap of seconds means the source published late; a gap of minutes means
    our own polling had stalled."""

    @staticmethod
    def _event():
        from datetime import UTC, datetime

        from signals.models import CompanyKey, NormalizedEvent

        return NormalizedEvent(
            source="fake",
            external_id="x",
            event_type="t",
            occurred_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
            company_key=CompanyKey(ticker="X"),
            summary="s",
        )

    async def test_events_record_who_found_them_and_the_gap_before(self) -> None:
        from tests.fakes import raw_with

        seen = []

        class Capture:
            async def process(self, event):
                from signals.pipeline.process import ProcessStats

                seen.append(dict(event.payload))
                return ProcessStats(seen=1)

        clock = FakeClock(OPEN)
        adapter = FakeAdapter("edgar_8k", script=[[], [raw_with([self._event()])]], interval=2.0)
        runner = AdapterRunner(adapter, Capture(), None, clock, CAL)  # type: ignore[arg-type]
        await runner.run(max_iterations=2)

        assert seen[0]["discovered_by"] == "edgar_8k"
        assert seen[0]["poll_gap_s"] == 2.0

    async def test_a_stalled_poller_shows_up_as_a_large_gap(self) -> None:
        from tests.fakes import raw_with

        seen = []

        class Capture:
            async def process(self, event):
                from signals.pipeline.process import ProcessStats

                seen.append(dict(event.payload))
                return ProcessStats(seen=1)

        clock = FakeClock(OPEN)
        script = [
            [],
            TransientSourceError("x"),
            TransientSourceError("x"),
            [raw_with([self._event()])],
        ]
        adapter = FakeAdapter("edgar_8k", script=script, interval=2.0)
        runner = AdapterRunner(adapter, Capture(), None, clock, CAL)  # type: ignore[arg-type]
        await runner.run(max_iterations=4)

        assert seen[0]["poll_gap_s"] >= 10.0, "the failed polls must widen the recorded gap"
