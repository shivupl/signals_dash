"""Rate limiter behaviour.

These tests use real time deliberately -- they are sub-second, and the whole
point is to prove the pacing an outside server will observe.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from signals.ratelimit import HostLimiter, Priority, TokenBucket


class TestTokenBucket:
    async def test_burst_is_capacity_then_it_paces(self) -> None:
        bucket = TokenBucket(rate=50, capacity=5)
        started = time.monotonic()
        for _ in range(5):
            await bucket.acquire()
        assert time.monotonic() - started < 0.05, "the initial burst must not wait"

        await bucket.acquire()  # the sixth needs a refill at 1/50s
        assert time.monotonic() - started >= 0.015

    async def test_stays_under_the_rate_under_concurrency(self) -> None:
        """The property SEC actually observes: N requests take at least N/rate."""
        rate = 40.0
        bucket = TokenBucket(rate=rate, capacity=1)
        n = 20
        started = time.monotonic()
        await asyncio.gather(*(bucket.acquire() for _ in range(n)))
        elapsed = time.monotonic() - started
        assert elapsed >= (n - 1) / rate * 0.9, f"{n} tokens in {elapsed:.3f}s exceeds {rate}/s"

    async def test_rejects_nonpositive_rate(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            TokenBucket(rate=0)


class TestPriority:
    async def test_high_priority_drains_first(self) -> None:
        """The case this exists for: a burst of document fetches must not delay
        the index poll, because the index poll is where latency is measured."""
        bucket = TokenBucket(rate=100, capacity=1)
        await bucket.acquire()  # drain the bucket so everyone queues

        order: list[str] = []

        async def take(label: str, priority: Priority) -> None:
            await bucket.acquire(priority)
            order.append(label)

        # Queue the low-priority work first, so FIFO alone would serve it first.
        lows = [asyncio.create_task(take(f"low{i}", Priority.LOW)) for i in range(4)]
        await asyncio.sleep(0.005)
        high = asyncio.create_task(take("high", Priority.HIGH))
        await asyncio.gather(high, *lows)

        assert order[0] == "high", f"low-priority work jumped the queue: {order}"


class TestCircuitBreaker:
    async def test_starts_closed(self) -> None:
        assert HostLimiter(rate=8).is_open is False

    async def test_trip_parks_the_host_then_recovers(self) -> None:
        limiter = HostLimiter(rate=8, cooldown=0.05)
        limiter.trip()
        assert limiter.is_open is True
        await asyncio.sleep(0.06)
        assert limiter.is_open is False, "the breaker must reopen on its own"

    async def test_reset_clears_immediately(self) -> None:
        limiter = HostLimiter(rate=8, cooldown=60)
        limiter.trip()
        limiter.reset()
        assert limiter.is_open is False

    async def test_slot_is_an_async_context_manager(self) -> None:
        limiter = HostLimiter(rate=100)
        async with limiter.slot(Priority.LOW):
            pass
