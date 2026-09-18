"""A shared token bucket with priority lanes.

SEC publishes a 10 req/s ceiling and enforces it with ~10-minute IP bans, so the
limit has to be global: four index pollers and a burst of Form 4 hydrations are
one client as far as sec.gov is concerned, and a per-adapter limiter would let
them sum past the ceiling.

The lanes matter as much as the rate. After 16:00 ET a wave of filings can queue
hundreds of document fetches; without priority those would sit in front of the
index polls, and the index poll *is* the latency metric -- it is the moment we
know a filing exists. HIGH always drains before LOW.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from dataclasses import dataclass, field
from enum import IntEnum
from types import TracebackType

from .clock import Clock, SystemClock


class Priority(IntEnum):
    """Lower sorts first."""

    HIGH = 0  # index polls: the thing latency is measured from
    LOW = 1   # document hydration: important, never urgent


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    future: asyncio.Future[None] = field(compare=False)


class TokenBucket:
    """Async token bucket. Capacity is the burst; rate is the steady state."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._clock = clock or SystemClock()
        self._tokens = self.capacity
        self._updated = self._clock.monotonic()
        self._waiters: list[_Waiter] = []
        self._seq = itertools.count()
        self._lock = asyncio.Lock()
        self._wake: asyncio.Task[None] | None = None

    def _refill(self) -> None:
        now = self._clock.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self, priority: Priority = Priority.HIGH) -> None:
        """Wait for one token. Higher-priority waiters are served first."""
        while True:
            async with self._lock:
                self._refill()
                # Take the token only if nobody more important is queued ahead.
                if self._tokens >= 1 and not self._has_higher_priority_waiter(priority):
                    self._tokens -= 1
                    return
                fut = asyncio.get_running_loop().create_future()
                waiter = _Waiter(int(priority), next(self._seq), fut)
                heapq.heappush(self._waiters, waiter)
                self._ensure_pump()
            await waiter.future

    def _has_higher_priority_waiter(self, priority: Priority) -> bool:
        return bool(self._waiters and self._waiters[0].priority < int(priority))

    def _ensure_pump(self) -> None:
        if self._wake is None or self._wake.done():
            self._wake = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Wake waiters in priority order as tokens become available."""
        while True:
            async with self._lock:
                self._refill()
                if not self._waiters:
                    return
                if self._tokens >= 1:
                    waiter = heapq.heappop(self._waiters)
                    if not waiter.future.done():
                        waiter.future.set_result(None)
                    continue
                deficit = 1 - self._tokens
                delay = deficit / self.rate
            await self._clock.sleep(delay)


class HostLimiter:
    """One bucket per host, plus a circuit breaker.

    When SEC starts returning 403 or 429, continuing to poll is how a soft
    throttle becomes a hard ban. The breaker parks the whole host for a cooldown
    while leaving every other adapter running -- one source failing must never
    stop the others.
    """

    def __init__(
        self,
        rate: float,
        cooldown: float = 600.0,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._bucket = TokenBucket(rate, clock=self._clock)
        self._cooldown = cooldown
        self._blocked_until: float | None = None

    @property
    def is_open(self) -> bool:
        """True when the breaker has tripped and the host is parked."""
        if self._blocked_until is None:
            return False
        if self._clock.monotonic() >= self._blocked_until:
            self._blocked_until = None
            return False
        return True

    def trip(self, cooldown: float | None = None) -> None:
        self._blocked_until = self._clock.monotonic() + (cooldown or self._cooldown)

    def reset(self) -> None:
        self._blocked_until = None

    async def acquire(self, priority: Priority = Priority.HIGH) -> None:
        await self._bucket.acquire(priority)

    def slot(self, priority: Priority = Priority.HIGH) -> _Slot:
        return _Slot(self, priority)


class _Slot:
    def __init__(self, limiter: HostLimiter, priority: Priority) -> None:
        self._limiter = limiter
        self._priority = priority

    async def __aenter__(self) -> None:
        await self._limiter.acquire(self._priority)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None
