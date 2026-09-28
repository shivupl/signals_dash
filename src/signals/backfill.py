"""Load months of filing history, so the scoring can be measured instead of argued.

Every constant in ``scoring/tables.py`` came from reasoning and none from evidence.
Judging them needs a corpus: a year or two of filings scored by the current rules,
with prices beside them. This builds that corpus.

It reuses the live reconciliation path rather than reimplementing it --
``EdgarBackfillAdapter`` already walks a company's submissions, hydrates Form 4 and
Form 144 documents and emits under the original source names, so a backfilled event
is indistinguishable from one caught live and dedupes against it. The only thing
this adds is reach: a window measured in months, older shards when
``filings.recent`` does not stretch that far, and per-company progress.

**Prices are loaded first, on purpose.** ``price_at`` for an event older than half
an hour comes from ``close_on_or_before`` -- the close of its own market date. Fill
the filings first and every historical flag gets a NULL price, which is the number
the whole exercise depends on.

Two limitations worth stating rather than discovering later.

**Survivorship.** ``company`` is seeded from ``company_tickers.json``, which lists
today's filers with tickers. Anything delisted or acquired before that snapshot was
never in the table, so it cannot appear here. This does not bias a *materiality*
study -- whether a 4.02 deserved a flag has nothing to do with the company's later
fate -- but it does bias any forward-return study upward, because the companies that
died are missing. Point-in-time index membership is recoverable from the
constituents file's own git history if that ever matters enough.

**Splits.** ``price_daily`` now carries ``adj_close`` beside ``close``: raw for
"what did it cost", adjusted for "what did it return". A return computed from
``close`` across a split is wrong by the split ratio.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .adapters.base import FetchContext
from .adapters.edgar_backfill import (
    SUBMISSIONS_URL,
    EdgarBackfillAdapter,
    recent_filings,
    shard_filings,
    shard_names,
)
from .clock import Clock, SystemClock
from .http import SourceClient
from .models import NormalizedEvent
from .prices import PriceService
from .ratelimit import Priority
from .store.base import Store

log = logging.getLogger(__name__)

#: Well under the 10 req/s ceiling. The live worker may be polling at the same
#: time and the two processes do not share a token bucket, so the backfill takes
#: the smaller half of the budget.
DEFAULT_RATE = 4.0

#: yfinance takes a period, and a day count is the shape it wants.
_DAYS_PER_MONTH = 31


@dataclass
class Progress:
    companies: int = 0
    filings: int = 0
    stored: int = 0
    duplicates: int = 0
    price_rows: int = 0
    shards_fetched: int = 0
    failures: int = 0
    per_source: dict[str, int] = field(default_factory=dict)

    def note(self, source: str) -> None:
        self.per_source[source] = self.per_source.get(source, 0) + 1


async def load_prices(
    store: Store, prices: PriceService, companies: Sequence[Any], months: int, progress: Progress
) -> None:
    """Daily closes for the window, raw and adjusted, before any filing lands."""
    days = months * _DAYS_PER_MONTH
    by_ticker = {c.ticker: c for c in companies if c.ticker}
    # yfinance batches, but a 500-ticker request is fragile; chunks keep a failure
    # local to its own slice.
    tickers = sorted(by_ticker)
    for start in range(0, len(tickers), 40):
        chunk = tickers[start : start + 40]
        bars = await prices.daily_closes(chunk, days=days)
        for ticker, points in bars.items():
            company = by_ticker.get(ticker)
            if company is None:
                continue
            for day, bar in points.items():
                await store.upsert_price_daily(company.id, day, bar.close, bar.adj_close)
                progress.price_rows += 1
        log.info(
            "prices %d/%d tickers, %d rows", min(start + 40, len(tickers)), len(tickers),
            progress.price_rows,
        )


async def company_filings(
    ctx: FetchContext, cik: str, name: str, since: datetime, progress: Progress
) -> list[dict[str, Any]]:
    """Every wanted filing for one company since ``since``, shards included."""
    payload = await ctx.http.get_bytes(SUBMISSIONS_URL.format(cik=cik), priority=Priority.LOW)
    filings = recent_filings(payload, since)

    # Only reach for shards when `recent` does not already cover the window: its
    # oldest entry older than the cutoff means nothing is missing.
    oldest = min((f["accepted"] for f in filings), default=None)
    covered = oldest is not None and oldest <= since + timedelta(days=1)
    if not covered:
        for shard in shard_names(payload, since):
            try:
                body = await ctx.http.get_bytes(
                    f"https://data.sec.gov/submissions/{shard}", priority=Priority.LOW
                )
            except Exception as exc:  # noqa: BLE001 -- one shard is not the run
                progress.failures += 1
                log.warning("shard %s failed: %s", shard, exc)
                continue
            progress.shards_fetched += 1
            filings.extend(shard_filings(body, since, cik=cik, name=name))
    return filings


class Backfiller:
    """Drives the reconciliation adapter over a long window, company by company."""

    def __init__(
        self,
        store: Store,
        processor: Any,
        http: SourceClient,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._processor = processor
        self._http = http
        self._clock = clock or SystemClock()
        self._adapter = EdgarBackfillAdapter()

    async def run(self, companies: Sequence[Any], months: int, progress: Progress) -> Progress:
        since = self._clock.now() - timedelta(days=months * _DAYS_PER_MONTH)
        log.info(
            "backfilling %d companies since %s", len(companies), since.date().isoformat()
        )
        for index, company in enumerate(companies, start=1):
            if not company.cik:
                continue
            ctx = FetchContext(
                http=self._http,
                clock=self._clock,
                # One company at a time: the adapter hydrates only watched issuers,
                # and this is the company we are asking about.
                watched_ciks=frozenset({company.cik}),
                state={},
            )
            try:
                filings = await company_filings(
                    ctx, company.cik, company.name or "", since, progress
                )
            except Exception as exc:  # noqa: BLE001 -- one company is not the run
                progress.failures += 1
                log.warning("submissions for %s failed: %s", company.ticker or company.cik, exc)
                continue

            stored_before = progress.stored
            for event in await self._events(ctx, filings):
                progress.note(event.source)
                stats = await self._processor.process(event)
                progress.stored += stats.stored
                progress.duplicates += 1 - stats.stored
            progress.companies += 1
            progress.filings += len(filings)
            log.info(
                "[%d/%d] %s %d filings, %d new",
                index,
                len(companies),
                company.ticker or company.cik,
                len(filings),
                progress.stored - stored_before,
            )
        await self._processor.drain()
        return progress

    async def _events(
        self, ctx: FetchContext, filings: Sequence[dict[str, Any]]
    ) -> list[NormalizedEvent]:
        """Hydrate and normalize through the adapter, so the corpus matches live."""
        raws = await self._adapter.from_filings(ctx, filings)
        return [event for raw in raws for event in self._adapter.normalize(raw)]


async def backfill(
    dsn: str,
    *,
    months: int,
    universe: str | None,
    tickers: Sequence[str] = (),
    limit: int | None = None,
    skip_prices: bool = False,
    rate: float = DEFAULT_RATE,
    user_agent: str,
) -> Progress:
    from .bus.memory_bus import MemoryBus
    from .pipeline.process import Processor
    from .prices import YFinanceProvider
    from .resolve.resolver import Resolver
    from .store.pg import PgStore

    store = await PgStore.connect(dsn)
    clock = SystemClock()
    progress = Progress()
    try:
        companies = await store.watched_companies(universe)
        if tickers:
            wanted = {t.strip().upper() for t in tickers}
            companies = [c for c in companies if (c.ticker or "").upper() in wanted]
        if limit is not None:
            companies = companies[:limit]
        if not companies:
            log.warning("no companies matched; nothing to do")
            return progress

        if not skip_prices:
            await load_prices(
                store, PriceService(YFinanceProvider()), companies, months, progress
            )

        # A memory bus, deliberately: nothing about two-year-old filings belongs on
        # the websocket, and publishing thousands of them would flood every client.
        processor = Processor(
            store,
            Resolver(store),
            MemoryBus(),
            flag_threshold=0,
            clock=clock,
            prices=PriceService(YFinanceProvider()),
        )
        http = SourceClient(user_agent, clock=clock, rate=rate)
        try:
            await Backfiller(store, processor, http, clock).run(companies, months, progress)
        finally:
            await http.aclose()
        return progress
    finally:
        await store.close()


def report(progress: Progress) -> str:
    lines = [
        "",
        f"companies       {progress.companies}",
        f"filings seen    {progress.filings}",
        f"events stored   {progress.stored}",
        f"duplicates      {progress.duplicates}",
        f"price rows      {progress.price_rows}",
        f"shards fetched  {progress.shards_fetched}",
        f"failures        {progress.failures}",
    ]
    if progress.per_source:
        lines.append("per source:")
        for source, count in sorted(progress.per_source.items()):
            lines.append(f"  {source:14} {count}")
    return "\n".join(lines)


#: Score bands for the corpus report. Chosen to fall on the thresholds that matter:
#: 0 is recorded-only, 30 is the watchlist's bar, 50 the index view's, 85 rule-only.
BANDS: tuple[tuple[int, int], ...] = (
    (0, 0),
    (1, 19),
    (20, 29),
    (30, 49),
    (50, 84),
    (85, 100),
)


def corpus_report(rows: Sequence[dict[str, Any]], threshold: int) -> str:
    """What the corpus is, and what the threshold would have meant against it.

    ``rows`` is one record per (source, score) from ``store.corpus_shape``. The
    number worth having is the last line: flags per market day at the threshold
    actually in use, measured rather than guessed.
    """
    if not rows:
        return "corpus is empty"

    total = sum(int(r["events"]) for r in rows)
    priced = sum(int(r["priced"]) for r in rows)
    earliest = min(r["earliest"] for r in rows)
    latest = max(r["latest"] for r in rows)
    days = max((int(r["days"]) for r in rows), default=0)
    lines = [
        "",
        f"events          {total}",
        f"coverage        {earliest} to {latest}",
        f"with price_at   {priced} ({priced / total * 100:.1f}%)",
        "",
        "per source:",
    ]

    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(str(row["source"]), []).append(row)
    for source, group in sorted(by_source.items()):
        count = sum(int(r["events"]) for r in group)
        flags = sum(int(r["events"]) for r in group if int(r["score"]) >= threshold)
        lines.append(
            f"  {source:14} {count:6}  flags {flags:5}"
            f"  {min(r['earliest'] for r in group)} to {max(r['latest'] for r in group)}"
        )

    lines += ["", "score bands:"]
    for low, high in BANDS:
        count = sum(int(r["events"]) for r in rows if low <= int(r["score"]) <= high)
        label = f"{low}" if low == high else f"{low}-{high}"
        marker = " <- flagged" if low >= threshold else ""
        lines.append(f"  {label:>7} {count:6}  {count / total * 100:5.1f}%{marker}")

    flags = sum(int(r["events"]) for r in rows if int(r["score"]) >= threshold)
    # Distinct market days seen, not calendar days: weekends file nothing and would
    # flatter the average.
    per_day = flags / days if days else 0.0
    lines += [
        "",
        f"at threshold {threshold}: {flags} flags over {days} market days"
        f" = {per_day:.1f} flags/day",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    import argparse
    import logging as logging_module

    from .config import Settings

    # A backfill runs for tens of minutes. Without this its progress logs go
    # nowhere -- the first long run looked hung for eight minutes while it was
    # working fine, and "is it stuck or slow" is not a question to leave open.
    logging_module.basicConfig(
        level=logging_module.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    logging_module.getLogger("httpx").setLevel(logging_module.WARNING)
    logging_module.getLogger("yfinance").setLevel(logging_module.ERROR)

    parser = argparse.ArgumentParser(description="Load filing history into the database.")
    parser.add_argument("--months", type=int, default=24)
    parser.add_argument(
        "--universe",
        default="core",
        help="core, sp500, or all (default: core -- the hand-picked watchlist)",
    )
    parser.add_argument("--tickers", default="", help="comma-separated, overrides --universe")
    parser.add_argument("--limit", type=int, default=None, help="first N companies only")
    parser.add_argument("--skip-prices", action="store_true")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE, help="requests per second")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="describe the corpus already stored and fetch nothing",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env(require_sec_user_agent=not args.report_only)
    universe = None if args.universe == "all" else args.universe

    if not args.report_only:
        progress = asyncio.run(
            backfill(
                settings.database_url,
                months=args.months,
                universe=universe,
                tickers=[t for t in args.tickers.split(",") if t.strip()],
                limit=args.limit,
                skip_prices=args.skip_prices,
                rate=args.rate,
                user_agent=settings.sec_user_agent,
            )
        )
        print(report(progress))

    print(asyncio.run(_describe(settings.database_url, universe)))
    return 0


async def _describe(dsn: str, universe: str | None) -> str:
    from .store.pg import PgStore

    store = await PgStore.connect(dsn)
    try:
        threshold = int(await store.get_setting("flag_threshold") or 30)
        return corpus_report(await store.corpus_shape(universe), threshold)
    finally:
        await store.close()
