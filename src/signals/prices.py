"""Prices: annotation, never execution.

Two jobs. Stamp ``price_at`` when a flag fires -- it cannot be reconstructed
later, and without it the outcome log planned for Phase 2 is guesswork. And keep
daily closes for the watchlist, which feed the rail's weekly change and the
feed's "since" figure.

yfinance is unofficial, synchronous, and breaks without warning. So it sits
behind a protocol, runs in a thread, and every call is best-effort with a
timeout: a price outage may leave a number blank, and must never delay or block
a flag.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

QUOTE_TIMEOUT = 8.0
BULK_TIMEOUT = 60.0


@dataclass(frozen=True, slots=True)
class Bar:
    """One day, two prices.

    ``close`` is what was printed: the number a flag's ``price_at`` means and the
    one the UI shows. ``adj_close`` is back-adjusted for splits and dividends, and
    is the only one safe to compute a return from -- a 10-for-1 split makes a raw
    series look like a 90% collapse. Neither can be derived from the other after
    the fact, so both are carried.
    """

    close: Decimal
    adj_close: Decimal | None = None


class PriceProvider(Protocol):
    """Synchronous on purpose: implementations wrap blocking libraries."""

    def quote(self, ticker: str) -> Decimal | None: ...
    def daily_closes(self, tickers: Sequence[str], days: int) -> dict[str, dict[date, Bar]]: ...
    def next_earnings(self, ticker: str) -> date | None: ...


class YFinanceProvider:
    def quote(self, ticker: str) -> Decimal | None:
        import yfinance as yf

        price = yf.Ticker(ticker).fast_info["last_price"]
        return _decimal(price)

    def daily_closes(self, tickers: Sequence[str], days: int) -> dict[str, dict[date, Bar]]:
        import yfinance as yf

        if not tickers:
            return {}
        # auto_adjust=False keeps Close raw *and* adds Adj Close, so one request
        # yields both. Fetching the adjusted series separately would double the
        # calls for a column yfinance already returned.
        frame = yf.download(
            list(tickers),
            period=f"{days}d",
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if frame is None or frame.empty:
            return {}
        closes = frame["Close"]
        levels = frame.columns.get_level_values(0) if frame.columns.nlevels > 1 else frame.columns
        adjusted = frame["Adj Close"] if "Adj Close" in set(levels) else None
        out: dict[str, dict[date, Bar]] = {}
        # A single ticker comes back as a Series, several as a DataFrame.
        columns = [tickers[0]] if getattr(closes, "ndim", 2) == 1 else list(closes.columns)
        for ticker in columns:
            series = closes if getattr(closes, "ndim", 2) == 1 else closes[ticker]
            adj_series = None
            if adjusted is not None:
                adj_series = adjusted if getattr(adjusted, "ndim", 2) == 1 else adjusted[ticker]
            points: dict[date, Bar] = {}
            for stamp, value in series.items():
                close = _decimal(value)
                if close is None:
                    continue
                adj = _decimal(adj_series.get(stamp)) if adj_series is not None else None
                # The index date is already the market date in ET -- never derive
                # it from a UTC timestamp, which is off by one after hours.
                points[stamp.date()] = Bar(close=close, adj_close=adj)
            if points:
                out[str(ticker)] = points
        return out

    def next_earnings(self, ticker: str) -> date | None:
        import yfinance as yf

        calendar = yf.Ticker(ticker).calendar
        dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
        upcoming = sorted(d for d in (dates or []) if isinstance(d, date) and d >= date.today())  # noqa: DTZ011
        return upcoming[0] if upcoming else None


class PriceService:
    """Async, time-boxed, exception-proof access to a provider."""

    def __init__(self, provider: PriceProvider) -> None:
        self._provider = provider

    async def quote(self, ticker: str) -> Decimal | None:
        return await self._guard(self._provider.quote, ticker, limit=QUOTE_TIMEOUT)

    async def daily_closes(
        self, tickers: Sequence[str], days: int = 10
    ) -> dict[str, dict[date, Bar]]:
        result = await self._guard(
            self._provider.daily_closes, tickers, days, limit=BULK_TIMEOUT
        )
        return result or {}

    async def next_earnings(self, ticker: str) -> date | None:
        return await self._guard(self._provider.next_earnings, ticker, limit=QUOTE_TIMEOUT)

    async def _guard(self, fn: Callable[..., T], *args: object, limit: float) -> T | None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), limit)
        except Exception as exc:  # noqa: BLE001 -- a price is never worth an outage
            log.warning("price lookup failed (%s): %s", getattr(fn, "__name__", fn), exc)
            return None


def _decimal(value: object) -> Decimal | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN or nonsense
        return None
    return Decimal(str(round(number, 4)))
