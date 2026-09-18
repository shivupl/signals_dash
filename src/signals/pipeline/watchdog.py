"""Staleness watchdog: a quiet source is itself a signal.

An empty feed is ambiguous -- nothing happened, or the thing that would have told
you is broken. So when an adapter has not completed a successful poll for thirty
minutes of ingest time, that fact is put *in the feed*, where you are already
looking, rather than in a log you are not.

It raises once and then stays silent until the source recovers, and says so when
it does.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime

from ..clock import Clock, MarketCalendar
from ..models import CompanyKey, NormalizedEvent
from .process import Processor
from .runner import AdapterRunner

log = logging.getLogger(__name__)

STALE_AFTER_MINUTES = 30.0
CHECK_EVERY = 60.0


class Watchdog:
    def __init__(
        self,
        runners: Sequence[AdapterRunner],
        processor: Processor,
        clock: Clock,
        calendar: MarketCalendar,
    ) -> None:
        self._runners = runners
        self._processor = processor
        self._clock = clock
        self._calendar = calendar
        self._started = clock.now()
        self._stale: set[str] = set()

    async def check(self) -> list[str]:
        """One pass. Returns the names of adapters whose state changed."""
        now = self._clock.now()
        changed: list[str] = []
        for runner in self._runners:
            name = runner.adapter.name
            since = runner.health.last_success or self._started
            quiet = self._calendar.ingest_minutes_between(since, now)
            if quiet >= STALE_AFTER_MINUTES and name not in self._stale:
                self._stale.add(name)
                changed.append(name)
                await self._emit(
                    name, since, now, stale=True, error=runner.health.last_error, quiet=quiet
                )
            elif quiet < STALE_AFTER_MINUTES and name in self._stale:
                self._stale.discard(name)
                changed.append(name)
                await self._emit(name, since, now, stale=False, error=None, quiet=quiet)
        return changed

    async def _emit(
        self,
        name: str,
        since: datetime,
        now: datetime,
        *,
        stale: bool,
        error: str | None,
        quiet: float,
    ) -> None:
        if stale:
            headline = f"Source quiet: {name} has not polled successfully in {quiet:.0f} min"
            detail = f"system · last error: {error}" if error else "system · no error recorded"
            log.error("watchdog: %s stale since %s (%s)", name, since.isoformat(), error)
        else:
            headline = f"Source recovered: {name}"
            detail = "system · polling normally again"
            log.info("watchdog: %s recovered", name)

        await self._processor.process(
            NormalizedEvent(
                source="system",
                # One row per episode: keyed on when the source last worked.
                external_id=f"{'stale' if stale else 'recovered'}:{name}:{since.isoformat()}",
                event_type="source_stale" if stale else "source_recovered",
                occurred_at=now,
                company_key=CompanyKey(),
                summary=headline,
                payload={
                    # Shown to everyone, like a market-wide halt: it is about no
                    # company, so it bypasses resolution and the watchlist filter.
                    "market_wide": True,
                    "severity": 70 if stale else 30,
                    "adapter": name,
                    "headline": headline,
                    "detail": detail,
                },
            )
        )

    def _log_health(self) -> None:
        """One line every ten minutes: enough to see a trend, not enough to drown."""
        for runner in self._runners:
            h = runner.health
            log.info(
                "health adapter=%s ok_iterations=%d consecutive_failures=%d stored=%d published=%d",
                h.name,
                h.iterations,
                h.consecutive_failures,
                h.stats.stored,
                h.stats.published,
            )

    async def run(self, max_iterations: int | None = None) -> None:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            try:
                await self.check()
                if iterations % 10 == 0:
                    self._log_health()
            except Exception:  # noqa: BLE001 -- the watchdog must outlive what it watches
                log.exception("watchdog check failed")
            await self._clock.sleep(CHECK_EVERY)
