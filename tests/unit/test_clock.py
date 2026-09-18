"""Calendar and timezone arithmetic.

These look like trivia until November, when a wrong answer makes the staleness
watchdog cry wolf all weekend and the latency numbers drift by an hour.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from signals.clock import (
    EASTERN,
    UTC,
    MarketCalendar,
    SystemClock,
    eastern_to_utc,
    to_utc,
)

CAL = MarketCalendar.load()


def et(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=EASTERN)


class TestTradingDays:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 9, 15), True),   # ordinary Tuesday
            (date(2026, 9, 19), False),  # Saturday
            (date(2026, 9, 20), False),  # Sunday
            (date(2026, 11, 26), False), # Thanksgiving
            (date(2026, 7, 3), False),   # Independence Day observed
            (date(2026, 11, 27), True),  # half day is still a trading day
        ],
    )
    def test_is_trading_day(self, day: date, expected: bool) -> None:
        assert CAL.is_trading_day(day) is expected

    def test_half_day_closes_early(self) -> None:
        assert CAL.close_time(date(2026, 11, 27)) == time(13, 0)
        assert CAL.close_time(date(2026, 11, 30)) == time(16, 0)


class TestMarketOpen:
    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (et(2026, 9, 15, 9, 29), False),  # one minute early
            (et(2026, 9, 15, 9, 30), True),   # the bell
            (et(2026, 9, 15, 15, 59), True),
            (et(2026, 9, 15, 16, 0), False),  # close is exclusive
            (et(2026, 11, 27, 12, 59), True), # half day, still open
            (et(2026, 11, 27, 13, 1), False), # half day, closed
        ],
    )
    def test_is_market_open(self, moment: datetime, expected: bool) -> None:
        assert CAL.is_market_open(moment) is expected

    def test_ingest_window_is_wider_than_the_session(self) -> None:
        """Filings do not stop at 16:00 -- the biggest 8-Ks land at 16:05."""
        after_close = et(2026, 9, 15, 17, 30)
        assert CAL.is_market_open(after_close) is False
        assert CAL.is_ingest_window(after_close) is True
        # ...but it does close eventually
        assert CAL.is_ingest_window(et(2026, 9, 15, 23, 0)) is False


class TestMarketMinutes:
    def test_within_one_session(self) -> None:
        assert CAL.market_minutes_between(et(2026, 9, 15, 10, 0), et(2026, 9, 15, 10, 30)) == 30

    def test_clamps_to_session_bounds(self) -> None:
        """Overnight looks like 17 hours on the wall clock and 30 minutes to the market."""
        overnight = CAL.market_minutes_between(et(2026, 9, 15, 15, 30), et(2026, 9, 16, 8, 0))
        assert overnight == 30

    def test_weekend_is_zero(self) -> None:
        """The case that makes the watchdog usable: Friday night to Sunday night."""
        assert CAL.market_minutes_between(et(2026, 9, 18, 17, 0), et(2026, 9, 20, 20, 0)) == 0

    def test_skips_a_holiday(self) -> None:
        """Wed 15:30 -> Fri 10:00 over Thanksgiving: 30 min Wed + 30 min Fri (half day)."""
        got = CAL.market_minutes_between(et(2026, 11, 25, 15, 30), et(2026, 11, 27, 10, 0))
        assert got == 60

    def test_full_session_is_390_minutes(self) -> None:
        assert CAL.market_minutes_between(et(2026, 9, 15, 0, 0), et(2026, 9, 15, 23, 59)) == 390

    def test_half_session_is_210_minutes(self) -> None:
        assert CAL.market_minutes_between(et(2026, 11, 27, 0, 0), et(2026, 11, 27, 23, 59)) == 210

    def test_reversed_range_is_zero_not_negative(self) -> None:
        assert CAL.market_minutes_between(et(2026, 9, 15, 12, 0), et(2026, 9, 15, 10, 0)) == 0

    def test_spans_dst_spring_forward(self) -> None:
        """Mar 8 2026 is a Sunday; the surrounding Fri->Mon span must still be two sessions."""
        got = CAL.market_minutes_between(et(2026, 3, 6, 0, 0), et(2026, 3, 9, 23, 59))
        assert got == 780


class TestEasternToUtc:
    def test_assembles_separate_date_and_time(self) -> None:
        """Nasdaq publishes HaltDate and HaltTime as two ET strings."""
        got = eastern_to_utc(date(2026, 9, 15), time(9, 41, 0))
        assert got == datetime(2026, 9, 15, 13, 41, tzinfo=UTC)  # EDT is UTC-4

    def test_standard_time_offset_differs(self) -> None:
        got = eastern_to_utc(date(2026, 12, 15), time(9, 41, 0))
        assert got == datetime(2026, 12, 15, 14, 41, tzinfo=UTC)  # EST is UTC-5

    def test_ambiguous_hour_on_fall_back(self) -> None:
        """01:30 happens twice on 2026-11-01. fold picks which one, and a halt
        genuinely can land there -- extended hours run through it."""
        first = eastern_to_utc(date(2026, 11, 1), time(1, 30), fold=0)
        second = eastern_to_utc(date(2026, 11, 1), time(1, 30), fold=1)
        assert first != second
        assert second - first == timedelta(hours=1)


class TestNaiveRejection:
    def test_to_utc_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            to_utc(datetime(2026, 9, 15, 12, 0))  # noqa: DTZ001 -- deliberately naive

    def test_market_open_rejects_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            CAL.is_market_open(datetime(2026, 9, 15, 12, 0))  # noqa: DTZ001


class TestSystemClock:
    def test_now_is_aware_utc(self) -> None:
        assert SystemClock().now().tzinfo is UTC

    def test_monotonic_advances(self) -> None:
        c = SystemClock()
        assert c.monotonic() <= c.monotonic()


def test_calendar_covers_the_current_year() -> None:
    """A tripwire for the yearly review.

    When the hardcoded table runs out, nothing crashes -- holidays quietly become
    trading days. This test is the thing that tells you.
    """
    from datetime import datetime as _dt

    this_year = _dt.now(tz=UTC).year
    years = {d.year for d in CAL.holidays}
    assert this_year in years, (
        f"config/market_calendar.yml has no holidays for {this_year}. "
        "Add the NYSE calendar for this year and the next."
    )
