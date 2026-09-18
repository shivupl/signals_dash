"""Wire everything together and run the adapters concurrently."""

from __future__ import annotations

import asyncio
import logging

from ..adapters.edgar_backfill import EdgarBackfillAdapter
from ..adapters.edgar_index import build_edgar_adapters
from ..adapters.halts import HaltsAdapter
from ..bus.base import Publisher
from ..bus.redis_bus import RedisPublisher
from ..clock import Clock, MarketCalendar, SystemClock
from ..config import Settings
from ..http import SourceClient
from ..prices import PriceService, YFinanceProvider
from ..resolve.resolver import Resolver
from ..store.pg import PgStore
from .price_loop import run_price_loop
from .process import Processor
from .runner import AdapterRunner
from .watchdog import Watchdog

log = logging.getLogger(__name__)


async def connect_with_retry(dsn: str, attempts: int = 30) -> PgStore:
    """A healthy container is not a ready pool."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await PgStore.connect(dsn)
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(min(0.5 * (attempt + 1), 3.0))
    raise RuntimeError(f"could not reach Postgres after {attempts} attempts: {last}")


async def run_worker(
    settings: Settings,
    *,
    clock: Clock | None = None,
    publisher: Publisher | None = None,
    max_iterations: int | None = None,
    watched_only: bool = True,
) -> list[AdapterRunner]:
    clock = clock or SystemClock()
    calendar = MarketCalendar.load()
    store = await connect_with_retry(settings.database_url)
    http = SourceClient(settings.sec_user_agent, clock=clock)
    bus = publisher or RedisPublisher(settings.redis_url)

    prices = PriceService(YFinanceProvider())

    processor = Processor(
        store=store,
        resolver=Resolver(store),
        publisher=bus,
        flag_threshold=settings.flag_threshold,
        watched_only=watched_only,
        started_at=clock.now(),
        prices=prices,
        clock=clock,
    )

    # Loaded once at startup: the Form 4 adapter uses it to decide whether a
    # filing is worth a document fetch, before anything is stored.
    watched_ciks = await store.watched_ciks()
    log.info("watching %d company CIKs", len(watched_ciks))

    # Pre-filters the halt feed. With --all the set is left empty, which the
    # adapter reads as "no filter".
    watched_tickers = await store.watched_tickers() if watched_only else frozenset()

    adapters = [*build_edgar_adapters(), HaltsAdapter(), EdgarBackfillAdapter()]
    runners = [
        AdapterRunner(
            adapter,
            processor,
            http,
            clock,
            calendar,
            watched_ciks=watched_ciks,
            watched_tickers=watched_tickers,
        )
        for adapter in adapters
    ]
    log.info("starting %d adapters: %s", len(runners), ", ".join(r.adapter.name for r in runners))

    try:
        # gather rather than a TaskGroup: a TaskGroup cancels its siblings when
        # one task raises, which is the opposite of the isolation this needs.
        await asyncio.gather(
            *(r.run(max_iterations) for r in runners),
            run_price_loop(store, prices, clock, calendar, max_iterations),
            Watchdog(runners, processor, clock, calendar).run(max_iterations),
        )
    finally:
        await http.aclose()
        await store.close()
    return runners
