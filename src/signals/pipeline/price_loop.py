"""Keep daily closes and earnings dates fresh for the watchlist.

Not an adapter: it produces no events. It writes the numbers the rail and the
"since" figure are computed from. Today's row is overwritten through the session
and its final value is the close.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ..clock import Clock, MarketCalendar
from ..prices import PriceService
from ..store.base import Store

log = logging.getLogger(__name__)

REFRESH_OPEN = 15 * 60  # during the session
REFRESH_CLOSED = 60 * 60  # otherwise
EARNINGS_EVERY = timedelta(hours=24)


async def refresh_prices(store: Store, prices: PriceService) -> int:
    """One pass. Returns the number of closes written."""
    companies = await store.watched_companies()
    by_ticker = {c.ticker: c for c in companies if c.ticker}
    closes = await prices.daily_closes(sorted(by_ticker), days=10)
    written = 0
    for ticker, points in closes.items():
        company = by_ticker.get(ticker)
        if company is None:
            continue
        for day, close in points.items():
            await store.upsert_price_daily(company.id, day, close)
            written += 1
    return written


async def refresh_earnings(store: Store, prices: PriceService) -> int:
    found = 0
    for company in await store.watched_companies():
        if not company.ticker:
            continue
        when = await prices.next_earnings(company.ticker)
        if when is not None:
            await store.set_next_earnings(company.id, when)
            found += 1
    return found


async def run_price_loop(
    store: Store,
    prices: PriceService,
    clock: Clock,
    calendar: MarketCalendar,
    max_iterations: int | None = None,
) -> None:
    last_earnings = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            written = await refresh_prices(store, prices)
            log.info("prices refreshed closes=%d", written)
            now = clock.now()
            if last_earnings is None or now - last_earnings >= EARNINGS_EVERY:
                found = await refresh_earnings(store, prices)
                last_earnings = now
                log.info("earnings dates refreshed found=%d", found)
        except Exception:  # noqa: BLE001 -- prices are annotation; never take the worker down
            log.exception("price refresh failed")
        is_open = calendar.is_market_open(clock.now())
        await clock.sleep(REFRESH_OPEN if is_open else REFRESH_CLOSED)
