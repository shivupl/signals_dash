"""Test doubles.

FakeClock is the important one. It lets a test drive many simulated hours of the
adapter loop in milliseconds, and -- more usefully -- assert on exactly how long
the runner decided to wait, which is how backoff and market-hours gating get
verified at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from signals.adapters.base import FetchContext
from signals.bus.base import BusMessage
from signals.models import NormalizedEvent, RawEvent


class FakeClock:
    """Time advances only when a test says so, or when the code under test sleeps."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
        self._monotonic = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds
        self._now += timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        self._now = moment


class FakeAdapter:
    """Scripted adapter. Each script entry is either events to yield or an
    exception to raise, so a test can say exactly how a source misbehaves."""

    market_hours_only = False

    def __init__(
        self,
        name: str = "fake",
        script: Sequence[Any] | None = None,
        interval: float = 1.0,
    ) -> None:
        self.name = name
        self.interval = interval
        self.script = list(script or [])
        self.calls = 0

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        self.calls += 1
        if not self.script:
            return []
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return list(step)

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        return list(raw.payload.get("events", []))


def raw_with(events: Sequence[NormalizedEvent]) -> RawEvent:
    return RawEvent(
        source="fake",
        external_id="x",
        url=None,
        fetched_at=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        payload={"events": list(events)},
    )


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[BusMessage] = []

    async def publish(self, message: BusMessage) -> None:
        self.messages.append(message)
