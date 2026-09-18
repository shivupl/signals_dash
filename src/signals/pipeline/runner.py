"""The per-adapter loop.

The try/except wrapping the whole body is the most important construct in this
file. One source failing must never stop the others: EDGAR going down should not
take the halt feed with it, and a parser bug in Form 4 should not stop 8-Ks
arriving.

Nothing here calls ``asyncio.sleep`` or ``datetime.now`` directly. Both go
through the injected clock, so tests drive many simulated hours in milliseconds
instead of waiting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..adapters.base import Adapter, FetchContext
from ..clock import Clock, MarketCalendar
from ..errors import PermanentSourceError, RateLimited, TransientSourceError
from ..http import SourceClient
from .process import Processor, ProcessStats

log = logging.getLogger(__name__)

MAX_BACKOFF = 60.0
BACKOFF_FACTOR = 4.0
#: Outside the ingest window there is nothing to find, but the loop keeps a slow
#: pulse so a source that breaks overnight is not discovered at 09:30.
IDLE_INTERVAL = 60.0


@dataclass
class AdapterHealth:
    name: str
    last_success: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    iterations: int = 0
    stats: ProcessStats = field(default_factory=ProcessStats)


class AdapterRunner:
    def __init__(
        self,
        adapter: Adapter,
        processor: Processor,
        http: SourceClient,
        clock: Clock,
        calendar: MarketCalendar,
        watched_ciks: frozenset[str] = frozenset(),
        watched_tickers: frozenset[str] = frozenset(),
    ) -> None:
        self.adapter = adapter
        self.health = AdapterHealth(name=adapter.name)
        self._processor = processor
        self._ctx = FetchContext(
            http=http,
            clock=clock,
            state={},
            watched_ciks=watched_ciks,
            watched_tickers=watched_tickers,
        )
        self._clock = clock
        self._calendar = calendar

    async def run(self, max_iterations: int | None = None) -> AdapterHealth:
        """Loop until cancelled. ``max_iterations`` exists so tests terminate."""
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            started = self._clock.monotonic()

            if not self._should_run_now():
                await self._clock.sleep(IDLE_INTERVAL)
                continue

            try:
                await self._iterate()
            except RateLimited as exc:
                self._record_failure(f"rate limited: {exc}")
                await self._clock.sleep(exc.retry_after or self._backoff())
                continue
            except TransientSourceError as exc:
                self._record_failure(str(exc), expected=True)
                await self._clock.sleep(self._backoff())
                continue
            except PermanentSourceError as exc:
                self._record_failure(str(exc))
                await self._clock.sleep(self._backoff())
                continue
            except Exception as exc:  # noqa: BLE001 -- isolation is the whole point
                log.exception("adapter=%s unexpected failure", self.adapter.name)
                self._record_failure(f"unexpected: {exc}")
                await self._clock.sleep(self._backoff())
                continue

            # Heartbeat advances only on a successful iteration, so the watchdog
            # measures "when did this source last actually work", not "is the
            # loop still spinning".
            self.health.last_success = self._clock.now()
            self.health.consecutive_failures = 0
            self.health.last_error = None
            self.health.iterations += 1

            elapsed = self._clock.monotonic() - started
            await self._clock.sleep(max(0.0, self.adapter.interval - elapsed))
        return self.health

    async def _iterate(self) -> None:
        for raw in await self.adapter.fetch(self._ctx):
            for event in self.adapter.normalize(raw):
                self.health.stats += await self._processor.process(event)

    def _should_run_now(self) -> bool:
        now = self._clock.now()
        if self.adapter.market_hours_only:
            return self._calendar.is_market_open(now)
        return self._calendar.is_ingest_window(now)

    def _backoff(self) -> float:
        """No penalty for a first failure; escalate only when it repeats.

        One-off failures are routine here -- a stale connection, a CDN challenge
        page -- and the next attempt almost always succeeds. Backing off after a
        single miss just widens the hole in coverage. A second consecutive
        failure is the first sign of a real problem, and that is where the
        multiplier starts.
        """
        repeats = max(0, self.health.consecutive_failures - 1)
        return min(self.adapter.interval * (BACKOFF_FACTOR**repeats), MAX_BACKOFF)

    def _record_failure(self, message: str, *, expected: bool = False) -> None:
        self.health.consecutive_failures += 1
        self.health.last_error = message
        # A single transient miss is normal operation, not news. It becomes a
        # warning when it repeats, and the watchdog covers sustained silence.
        level = (
            logging.INFO if expected and self.health.consecutive_failures == 1 else logging.WARNING
        )
        log.log(
            level,
            "adapter=%s failure=%s consecutive=%d",
            self.adapter.name,
            message,
            self.health.consecutive_failures,
        )
