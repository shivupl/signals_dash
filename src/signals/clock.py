"""Time, in one place.

Two rules hold this project together:

1. Nothing outside this module calls ``datetime.now()``, ``time.monotonic()`` or
   ``asyncio.sleep()``. Everything goes through a ``Clock``, so tests drive the
   adapter loops deterministically instead of sleeping in real time.

2. Every internal datetime is tz-aware UTC. ``America/New_York`` appears only at
   the parse boundary (EDGAR timestamps, Nasdaq halt times) and the render
   boundary (the UI). Between those two points, Eastern time does not exist.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import yaml

# Defined once, imported everywhere. Re-creating this per call site is how DST
# bugs get in.
EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
HALF_DAY_CLOSE = dtime(13, 0)

# Filings do not stop when the bell rings. EDGAR accepts until roughly 22:00 ET
# and the heaviest 8-K window is 16:00-18:00, so the ingest schedule is much
# wider than the trading session.
INGEST_START = dtime(6, 0)
INGEST_END = dtime(22, 0)

_CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "market_calendar.yml"


class Clock(Protocol):
    """The seam that makes the async pipeline testable."""

    def now(self) -> datetime:
        """Current time, tz-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring durations."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Production implementation. The only place real time enters the system."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class MarketCalendar:
    """NYSE holidays and half-days, read from config.

    A hardcoded table rather than a dependency: this is a personal tool watching
    40 tickers, and a yearly five-minute review beats carrying pandas-market-
    calendars and its transitive tree.
    """

    def __init__(self, holidays: set[date], half_days: set[date]) -> None:
        self.holidays = holidays
        self.half_days = half_days

    @classmethod
    def load(cls, path: Path | None = None) -> MarketCalendar:
        data = yaml.safe_load((path or _CALENDAR_PATH).read_text())
        return cls(
            holidays={_as_date(d) for d in data.get("holidays", [])},
            half_days={_as_date(d) for d in data.get("half_days", [])},
        )

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def close_time(self, d: date) -> dtime:
        return HALF_DAY_CLOSE if d in self.half_days else MARKET_CLOSE

    def is_market_open(self, moment: datetime) -> bool:
        et = _to_eastern(moment)
        if not self.is_trading_day(et.date()):
            return False
        return MARKET_OPEN <= et.time() < self.close_time(et.date())

    def is_ingest_window(self, moment: datetime) -> bool:
        """Wider than the session: filings land long before and after the bell."""
        et = _to_eastern(moment)
        if not self.is_trading_day(et.date()):
            return False
        return INGEST_START <= et.time() < INGEST_END

    def market_minutes_between(self, start: datetime, end: datetime) -> float:
        """Minutes of actual trading time between two instants."""
        return self._minutes_between(start, end, session_only=True)

    def ingest_minutes_between(self, start: datetime, end: datetime) -> float:
        """Minutes inside the 06:00-22:00 ET ingest window between two instants.

        What the staleness watchdog measures. Wall-clock minutes would cry wolf
        every weekend; session-only minutes would leave a source that dies at
        16:05 -- the start of the heaviest 8-K window -- undetected until the
        next morning.
        """
        return self._minutes_between(start, end, session_only=False)

    def _minutes_between(self, start: datetime, end: datetime, *, session_only: bool) -> float:
        if end <= start:
            return 0.0
        total = 0.0
        cursor = _to_eastern(start)
        stop = _to_eastern(end)
        while cursor.date() <= stop.date():
            day = cursor.date()
            if self.is_trading_day(day):
                if session_only:
                    open_at = _et_on(day, MARKET_OPEN)
                    close_at = _et_on(day, self.close_time(day))
                else:
                    open_at = _et_on(day, INGEST_START)
                    close_at = _et_on(day, INGEST_END)
                lo = max(cursor, open_at)
                hi = min(stop, close_at)
                if hi > lo:
                    total += (hi - lo).total_seconds() / 60.0
            cursor = _et_on(day + timedelta(days=1), dtime(0, 0))
        return total


def to_utc(moment: datetime) -> datetime:
    """Normalize any aware datetime to UTC. Rejects naive input."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return moment.astimezone(UTC)


def eastern_to_utc(d: date, t: dtime, *, fold: int = 0) -> datetime:
    """Assemble a separate ET date and time into a UTC instant.

    ``fold`` matters: Nasdaq publishes halt date and halt time as two strings, and
    extended-hours halts do land inside the repeated 01:00-02:00 hour on the
    November fall-back date. fold=0 picks the first (EDT) occurrence.
    """
    return datetime.combine(d, t, tzinfo=EASTERN).replace(fold=fold).astimezone(UTC)


def _to_eastern(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return moment.astimezone(EASTERN)


def _et_on(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t, tzinfo=EASTERN)


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"not a date: {value!r}")
