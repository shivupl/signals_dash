"""The watchdog: a quiet source is itself a signal."""

from __future__ import annotations

from datetime import UTC, datetime

from signals.clock import EASTERN, MarketCalendar
from signals.pipeline.runner import AdapterRunner
from signals.pipeline.watchdog import Watchdog
from tests.fakes import FakeAdapter, FakeClock

CAL = MarketCalendar.load()
TUESDAY_10AM = datetime(2026, 9, 15, 10, 0, tzinfo=EASTERN).astimezone(UTC)


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list = []

    async def process(self, event) -> None:
        self.events.append(event)


def rig(clock: FakeClock, names: list[str]):
    runners = [
        AdapterRunner(FakeAdapter(n), None, None, clock, CAL)  # type: ignore[arg-type]
        for n in names
    ]
    processor = RecordingProcessor()
    return runners, processor, Watchdog(runners, processor, clock, CAL)  # type: ignore[arg-type]


class TestStaleness:
    async def test_a_healthy_source_raises_nothing(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, dog = rig(clock, ["edgar_8k"])
        clock.advance(29 * 60)
        runners[0].health.last_success = clock.now()
        assert await dog.check() == []
        assert processor.events == []

    async def test_thirty_quiet_minutes_puts_an_event_in_the_feed(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, dog = rig(clock, ["edgar_8k", "halts"])
        clock.advance(31 * 60)
        runners[1].health.last_success = clock.now()  # halts is fine
        assert await dog.check() == ["edgar_8k"]
        event = processor.events[0]
        assert event.source == "system"
        assert event.event_type == "source_stale"
        assert "edgar_8k" in event.payload["headline"]

    async def test_it_raises_once_not_every_minute(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, dog = rig(clock, ["edgar_8k"])
        clock.advance(31 * 60)
        await dog.check()
        clock.advance(31 * 60)
        await dog.check()
        assert len(processor.events) == 1

    async def test_recovery_is_announced_and_rearms_the_alarm(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, dog = rig(clock, ["edgar_8k"])
        clock.advance(31 * 60)
        await dog.check()
        runners[0].health.last_success = clock.now()
        await dog.check()
        assert [e.event_type for e in processor.events] == ["source_stale", "source_recovered"]
        clock.advance(31 * 60)
        await dog.check()
        assert processor.events[-1].event_type == "source_stale"

    async def test_the_last_error_is_carried_into_the_feed(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_error = "www.sec.gov returned 403"
        clock.advance(31 * 60)
        await dog.check()
        assert "403" in processor.events[0].payload["detail"]

    async def test_it_reaches_everyone_like_a_market_wide_event(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, dog = rig(clock, ["edgar_8k"])
        clock.advance(31 * 60)
        await dog.check()
        assert processor.events[0].payload["market_wide"] is True
        assert not processor.events[0].company_key


class TestItDoesNotCryWolf:
    async def test_a_whole_weekend_of_silence_is_not_stale(self) -> None:
        """Wall-clock minutes would fire every Saturday morning. Last success at
        21:50 Friday leaves ten minutes of window; the weekend adds none."""
        clock = FakeClock(datetime(2026, 9, 18, 21, 50, tzinfo=EASTERN).astimezone(UTC))
        runners, processor, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_success = clock.now()
        clock.advance(48 * 3600)  # Sunday 21:50
        assert await dog.check() == []
        assert processor.events == []

    async def test_monday_morning_silence_does_count(self) -> None:
        clock = FakeClock(datetime(2026, 9, 18, 21, 50, tzinfo=EASTERN).astimezone(UTC))
        runners, _processor, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_success = clock.now()
        clock.advance(56 * 3600 + 40 * 60)  # Monday 06:30 ET: 10 + 30 ingest minutes
        assert await dog.check() == ["edgar_8k"]

    async def test_a_source_dying_after_the_close_is_still_caught_that_evening(self) -> None:
        """Why this measures ingest minutes, not session minutes: the heaviest
        8-K window starts at 16:00, and session-only time stops counting there."""
        clock = FakeClock(datetime(2026, 9, 15, 16, 5, tzinfo=EASTERN).astimezone(UTC))
        runners, processor, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_success = clock.now()
        clock.advance(35 * 60)
        assert await dog.check() == ["edgar_8k"]
        assert CAL.market_minutes_between(runners[0].health.last_success, clock.now()) == 0


class TestIngestMinutes:
    def test_counts_the_wide_window(self) -> None:
        start = datetime(2026, 9, 15, 5, 0, tzinfo=EASTERN)
        end = datetime(2026, 9, 15, 23, 0, tzinfo=EASTERN)
        assert CAL.ingest_minutes_between(start, end) == 16 * 60

    def test_skips_weekends_and_holidays(self) -> None:
        sat = datetime(2026, 9, 19, 0, 0, tzinfo=EASTERN)
        sun = datetime(2026, 9, 20, 23, 59, tzinfo=EASTERN)
        assert CAL.ingest_minutes_between(sat, sun) == 0
