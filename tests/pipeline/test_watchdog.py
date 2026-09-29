"""The watchdog: a quiet source is itself a signal -- said once, not nine times."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from signals.clock import EASTERN, MarketCalendar
from signals.pipeline.runner import AdapterRunner
from signals.pipeline.watchdog import Watchdog, human_duration, stale_after
from tests.fakes import FakeAdapter, FakeClock

CAL = MarketCalendar.load()
TUESDAY_10AM = datetime(2026, 9, 15, 10, 0, tzinfo=EASTERN).astimezone(UTC)


class RecordingProcessor:
    def __init__(self) -> None:
        self.events: list = []

    async def process(self, event) -> None:
        self.events.append(event)


class RecordingStore:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str, dict]] = []

    async def update_payload(self, source: str, external_id: str, patch: dict) -> int:
        self.updates.append((source, external_id, patch))
        return 1


def rig(clock: FakeClock, names: list[str]):
    runners = [
        AdapterRunner(FakeAdapter(n), None, None, clock, CAL)  # type: ignore[arg-type]
        for n in names
    ]
    processor, store = RecordingProcessor(), RecordingStore()
    dog = Watchdog(runners, processor, clock, CAL, store)  # type: ignore[arg-type]
    return runners, processor, store, dog


class TestStaleness:
    async def test_a_healthy_source_raises_nothing(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, _store, dog = rig(clock, ["edgar_8k"])
        clock.advance(29 * 60)
        runners[0].health.last_success = clock.now()
        assert await dog.check() == []
        assert processor.events == []

    async def test_thirty_quiet_minutes_raises_one_alarm(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, _store, dog = rig(clock, ["edgar_8k", "halts"])
        clock.advance(31 * 60)
        runners[1].health.last_success = clock.now()
        assert await dog.check() == ["edgar_8k"]
        event = processor.events[0]
        assert (event.source, event.event_type) == ("system", "source_stale")
        assert event.payload["state"] == "open"
        assert event.payload["headline"] == "edgar_8k degraded 31m"

    async def test_the_last_error_is_carried(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, _store, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_error = "www.sec.gov returned 403"
        clock.advance(31 * 60)
        await dog.check()
        assert "403" in processor.events[0].payload["detail"]


class TestOneRowPerOutage:
    """The first version wrote a new row per episode. On a host that kept dozing
    that put nine system rows in a feed with two real flags."""

    async def test_an_ongoing_outage_updates_in_place(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, store, dog = rig(clock, ["halts"])
        clock.advance(31 * 60)
        await dog.check()
        for _ in range(3):
            clock.advance(8 * 60)
            await dog.check()

        assert len(processor.events) == 1, "an ongoing outage minted new rows"
        assert len(store.updates) == 3
        assert {u[1] for u in store.updates} == {processor.events[0].external_id}

    async def test_the_duration_counts_up(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, _processor, store, dog = rig(clock, ["halts"])
        clock.advance(31 * 60)
        await dog.check()
        clock.advance(24 * 60)
        await dog.check()
        assert store.updates[-1][2]["headline"] == "halts degraded 55m"
        assert store.updates[-1][2]["minutes"] == 55.0

    async def test_recovery_closes_the_row_and_says_so_once(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, store, dog = rig(clock, ["edgar_form4"])
        clock.advance(31 * 60)
        await dog.check()
        clock.advance(10 * 60)
        runners[0].health.last_success = clock.now()
        assert await dog.check() == ["edgar_form4"]

        assert [e.event_type for e in processor.events] == ["source_stale", "source_recovered"]
        assert processor.events[1].payload["headline"] == "edgar_form4 recovered"
        closing = store.updates[-1][2]
        assert closing["state"] == "resolved"
        assert "was degraded for" in closing["headline"]

        await dog.check()
        assert len(processor.events) == 2, "recovery was announced twice"

    async def test_a_later_outage_is_a_new_row(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        runners, processor, _store, dog = rig(clock, ["edgar_8k"])
        clock.advance(31 * 60)
        await dog.check()
        runners[0].health.last_success = clock.now()
        await dog.check()
        clock.advance(31 * 60)
        await dog.check()
        stale = [e for e in processor.events if e.event_type == "source_stale"]
        assert len(stale) == 2
        assert stale[0].external_id != stale[1].external_id


class TestHostSuspension:
    """When wall-clock time jumps and the process clock did not, the machine was
    asleep. That is one fact, and it is not the sources' fault."""

    async def test_a_suspended_host_is_reported_once_as_itself(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, _store, dog = rig(clock, ["edgar_8k", "edgar_form4", "halts"])
        clock.advance(60)
        await dog.check()
        clock.set(clock.now() + timedelta(minutes=47))  # wall jumps, monotonic does not
        assert await dog.check() == []
        assert [e.event_type for e in processor.events] == ["host_suspended"]
        assert processor.events[0].payload["headline"] == "Host was suspended for 47m"

    async def test_sources_are_not_blamed_for_the_gap(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, _store, dog = rig(clock, ["edgar_8k", "halts"])
        clock.advance(60)
        await dog.check()
        clock.set(clock.now() + timedelta(minutes=47))
        await dog.check()
        clock.advance(60)
        await dog.check()
        assert [e.event_type for e in processor.events] == ["host_suspended"]

    async def test_a_source_still_dead_after_waking_is_caught(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, _store, dog = rig(clock, ["edgar_8k"])
        clock.advance(60)
        await dog.check()
        clock.set(clock.now() + timedelta(minutes=47))
        await dog.check()
        clock.advance(31 * 60)
        await dog.check()
        assert [e.event_type for e in processor.events] == ["host_suspended", "source_stale"]

    async def test_ordinary_time_passing_is_not_a_suspension(self) -> None:
        clock = FakeClock(TUESDAY_10AM)
        _runners, processor, _store, dog = rig(clock, ["edgar_8k"])
        for _ in range(5):
            clock.advance(60)
            await dog.check()
        assert processor.events == []


class TestItDoesNotCryWolf:
    async def test_a_whole_weekend_of_silence_is_not_stale(self) -> None:
        clock = FakeClock(datetime(2026, 9, 18, 21, 50, tzinfo=EASTERN).astimezone(UTC))
        runners, processor, _store, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_success = clock.now()
        clock.advance(48 * 3600)
        assert await dog.check() == []
        assert processor.events == []

    async def test_a_source_dying_after_the_close_is_caught_that_evening(self) -> None:
        """Why this measures ingest minutes, not session minutes."""
        clock = FakeClock(datetime(2026, 9, 15, 16, 5, tzinfo=EASTERN).astimezone(UTC))
        runners, _processor, _store, dog = rig(clock, ["edgar_8k"])
        runners[0].health.last_success = clock.now()
        clock.advance(35 * 60)
        assert await dog.check() == ["edgar_8k"]


class TestHelpers:
    def test_durations_read_like_a_person_wrote_them(self) -> None:
        assert human_duration(55) == "55m"
        assert human_duration(60) == "1h"
        assert human_duration(125) == "2h 05m"

    def test_slow_adapters_get_a_proportionate_limit(self) -> None:
        assert stale_after(2.0) == 30.0
        assert stale_after(1800.0) == 75.0

    def test_ingest_minutes(self) -> None:
        start = datetime(2026, 9, 15, 5, 0, tzinfo=EASTERN)
        end = datetime(2026, 9, 15, 23, 0, tzinfo=EASTERN)
        assert CAL.ingest_minutes_between(start, end) == 16 * 60


class TestDroughts:
    """"Is it responding" and "is it working" are different questions.

    13D/G answered every poll for nine months while matching nothing, because SEC
    had renamed the form. Every check the system had said healthy. This is the
    check that would have caught it -- and it needed the backfill first, because
    without history there is no such thing as "normally twelve a week".
    """

    @staticmethod
    def row(source: str, recent: int, per_week: float) -> dict[str, object]:
        return {"source": source, "recent": recent, "per_week": per_week, "historical": 500}

    def test_a_busy_source_gone_silent_is_a_drought(self) -> None:
        from signals.pipeline.watchdog import droughts

        found = droughts([self.row("edgar_13dg", 0, 12.0)], 14)
        assert [d.source for d in found] == ["edgar_13dg"]
        assert found[0].per_week == 12.0

    def test_a_source_still_producing_is_not(self) -> None:
        from signals.pipeline.watchdog import droughts

        assert droughts([self.row("edgar_8k", 40, 12.0)], 14) == []

    def test_one_event_is_enough_to_clear_it(self) -> None:
        """Zero is the trigger. A source at half its rate is a question, not an
        answer, and answering questions with alarms is how alarms get ignored."""
        from signals.pipeline.watchdog import droughts

        assert droughts([self.row("edgar_13dg", 1, 12.0)], 14) == []

    def test_a_genuinely_rare_source_is_never_judged_this_way(self) -> None:
        """Halts go weeks without one for a forty-name watchlist. Absence there
        carries no information, so it must not raise an alarm."""
        from signals.pipeline.watchdog import droughts

        assert droughts([self.row("halts", 0, 0.4)], 14) == []

    def test_a_source_with_no_history_is_not_judged(self) -> None:
        """A source added yesterday has no normal to fall short of."""
        from signals.pipeline.watchdog import droughts

        assert droughts([self.row("edgar_144", 0, 0.0)], 14) == []

    def test_the_alarm_says_what_normal_was(self) -> None:
        from signals.pipeline.watchdog import droughts

        drought = droughts([self.row("edgar_13dg", 0, 12.0)], 14)[0]
        assert drought.headline == "edgar_13dg has produced nothing in 14 days"
        assert "about 12 a week" in drought.detail
        assert drought.external_id == "drought:edgar_13dg", "one row per source, not per check"

    def test_several_dead_sources_are_reported_separately(self) -> None:
        from signals.pipeline.watchdog import droughts

        found = droughts(
            [self.row("edgar_13dg", 0, 12.0), self.row("edgar_8k", 0, 20.0)], 14
        )
        assert {d.source for d in found} == {"edgar_13dg", "edgar_8k"}
