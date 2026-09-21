"""Staleness watchdog: a quiet source is itself a signal.

An empty feed is ambiguous -- nothing happened, or the thing that would have told
you is broken. So when an adapter has not completed a successful poll for thirty
minutes of ingest time, that fact is recorded as a system event.

**One row per outage.** An outage is stored once and updated in place while it
lasts -- its duration counts up -- and a single "recovered" event is written when
it clears. The first version wrote a new row per episode, and on a host that kept
dozing that produced nine alarms for what was one fact.

**The host sleeping is not nine sources failing.** When wall-clock time jumps but
this process's monotonic clock did not, the machine was suspended. That is
recorded once, as what it is, and the per-source alarms are not raised for a gap
the sources had nothing to do with.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ..clock import Clock, MarketCalendar
from ..models import CompanyKey, NormalizedEvent
from ..store.base import Store
from .process import Processor
from .runner import AdapterRunner

log = logging.getLogger(__name__)

STALE_AFTER_MINUTES = 30.0
CHECK_EVERY = 60.0
#: Wall clock running this far ahead of the process clock between two checks
#: means the host was suspended in between.
SUSPENSION_SLACK_SECONDS = 90.0


def stale_after(interval_seconds: float) -> float:
    """Minutes of silence that count as stale for an adapter polling this often.

    Thirty minutes for anything fast. A slow adapter gets two and a half of its
    own intervals -- the half-hourly reconciliation sweep would otherwise trip a
    thirty-minute alarm every time it ran on schedule.
    """
    return max(STALE_AFTER_MINUTES, interval_seconds / 60.0 * 2.5)


def human_duration(minutes: float) -> str:
    minutes = max(0, round(minutes))
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m" if rest else f"{hours}h"


@dataclass
class Outage:
    adapter: str
    since: datetime
    external_id: str


class Watchdog:
    def __init__(
        self,
        runners: Sequence[AdapterRunner],
        processor: Processor,
        clock: Clock,
        calendar: MarketCalendar,
        store: Store | None = None,
    ) -> None:
        self._runners = runners
        self._processor = processor
        self._store = store
        self._clock = clock
        self._calendar = calendar
        self._started = clock.now()
        self._open: dict[str, Outage] = {}
        #: Silence before this instant is not held against any source.
        self._baseline = self._started
        self._last_wall = clock.now()
        self._last_mono = clock.monotonic()

    async def check(self) -> list[str]:
        """One pass. Returns the names of adapters whose state changed."""
        now = self._clock.now()
        if await self._note_suspension(now):
            # The gap belongs to the host. Judge the sources from here, once they
            # have had a chance to poll again, rather than blaming them for it.
            self._forgive_gap(now)
            return []

        changed: list[str] = []
        for runner in self._runners:
            name = runner.adapter.name
            since = max(runner.health.last_success or self._started, self._baseline)
            quiet = self._calendar.ingest_minutes_between(since, now)
            limit = stale_after(runner.adapter.interval)
            outage = self._open.get(name)

            if quiet >= limit and outage is None:
                outage = Outage(name, since, f"outage:{name}:{since.isoformat()}")
                self._open[name] = outage
                changed.append(name)
                await self._raise(outage, now, quiet, runner.health.last_error)
            elif quiet >= limit and outage is not None:
                await self._tick(outage, quiet, runner.health.last_error)
            elif quiet < limit and outage is not None:
                del self._open[name]
                changed.append(name)
                await self._clear(outage, now)
        return changed

    # --- host suspension ----------------------------------------------------

    async def _note_suspension(self, now: datetime) -> bool:
        wall = (now - self._last_wall).total_seconds()
        mono = self._clock.monotonic() - self._last_mono
        self._last_wall = now
        self._last_mono = self._clock.monotonic()
        lost = wall - mono
        if lost < SUSPENSION_SLACK_SECONDS:
            return False

        minutes = lost / 60.0
        log.warning("watchdog: host was suspended for %s", human_duration(minutes))
        await self._emit(
            external_id=f"suspended:{now.isoformat()}",
            event_type="host_suspended",
            occurred_at=now,
            severity=60,
            headline=f"Host was suspended for {human_duration(minutes)}",
            detail="system · nothing was polled in the gap; the reconciliation sweep catches up",
            extra={"adapter": "host", "state": "info", "minutes": round(minutes, 1)},
        )
        return True

    def _forgive_gap(self, now: datetime) -> None:
        self._baseline = now

    # --- outage lifecycle ---------------------------------------------------

    async def _raise(self, outage: Outage, now: datetime, quiet: float, error: str | None) -> None:
        log.error("watchdog: %s stale since %s (%s)", outage.adapter, outage.since, error)
        await self._emit(
            external_id=outage.external_id,
            event_type="source_stale",
            occurred_at=now,
            severity=70,
            headline=f"{outage.adapter} degraded {human_duration(quiet)}",
            detail=_detail(error),
            extra={
                "adapter": outage.adapter,
                "state": "open",
                "since": outage.since.isoformat(),
                "minutes": round(quiet, 1),
            },
        )

    async def _tick(self, outage: Outage, quiet: float, error: str | None) -> None:
        """Update the open outage's row in place. No new row, no new push."""
        if self._store is None:
            return
        await self._store.update_payload(
            "system",
            outage.external_id,
            {
                "minutes": round(quiet, 1),
                "headline": f"{outage.adapter} degraded {human_duration(quiet)}",
                "detail": _detail(error),
            },
        )

    async def _clear(self, outage: Outage, now: datetime) -> None:
        minutes = self._calendar.ingest_minutes_between(outage.since, now)
        log.info("watchdog: %s recovered after %s", outage.adapter, human_duration(minutes))
        if self._store is not None:
            await self._store.update_payload(
                "system",
                outage.external_id,
                {
                    "state": "resolved",
                    "resolved_at": now.isoformat(),
                    "minutes": round(minutes, 1),
                    "headline": f"{outage.adapter} was degraded for {human_duration(minutes)}",
                },
            )
        await self._emit(
            external_id=f"recovered:{outage.adapter}:{outage.since.isoformat()}",
            event_type="source_recovered",
            occurred_at=now,
            severity=30,
            headline=f"{outage.adapter} recovered",
            detail=f"system · after {human_duration(minutes)}",
            extra={"adapter": outage.adapter, "state": "info", "minutes": round(minutes, 1)},
        )

    async def _emit(
        self,
        *,
        external_id: str,
        event_type: str,
        occurred_at: datetime,
        severity: int,
        headline: str,
        detail: str,
        extra: dict[str, object],
    ) -> None:
        await self._processor.process(
            NormalizedEvent(
                source="system",
                external_id=external_id,
                event_type=event_type,
                occurred_at=occurred_at,
                company_key=CompanyKey(),
                summary=headline,
                payload={
                    # About no company, so it bypasses resolution and the
                    # watchlist filter, like a market-wide halt.
                    "market_wide": True,
                    "severity": severity,
                    "headline": headline,
                    "detail": detail,
                    **extra,
                },
            )
        )

    def _log_health(self) -> None:
        """One line per adapter every ten minutes: a trend, not a flood."""
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


def _detail(error: str | None) -> str:
    return f"system · last error: {error}" if error else "system · no error recorded"
