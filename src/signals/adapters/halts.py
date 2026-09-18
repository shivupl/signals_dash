"""Trading halts, from the Nasdaq Trader RSS feed.

A halt is one feed item that *mutates*: resumption fields are empty while the
halt is open and fill in when it lifts. Under ``unique (source, external_id)`` a
naive id would make the resume a duplicate of the halt, silently swallowed. So
one natural key yields two ids:

    halt:<key>     the halt itself
    resume:<key>   its resumption, keyed on the HALT, not the resumption time

The resume id hangs off the halt key deliberately. Quotes can resume minutes
before trading does, so a resumption timestamp is a moving target; the halt's own
fields never change.

When resumption is present, both events are emitted unconditionally. The halt
insert is a harmless no-op if it is already stored, and it means a cold start --
the app restarted and never saw the halt open -- needs no special branch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from ..clock import eastern_to_utc
from ..errors import TransientSourceError
from ..models import CompanyKey, NormalizedEvent, RawEvent
from ..parsers.halts_rss import HaltItem, parse_halts
from ..ratelimit import Priority
from ..scoring.halts import describe_halt
from ..scoring.tables import MARKET_WIDE_REASONS, VOLATILITY_REASONS
from .base import FetchContext

FEED_URL: Final[str] = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
MAX_TRACKED: Final[int] = 5000


def looks_like_feed(payload: bytes) -> bool:
    head = payload[:300].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith(b"<?xml") or head.startswith(b"<rss")


def halt_instant(item: HaltItem) -> datetime:
    # fold=0: a halt inside the repeated 01:00-02:00 hour on the November
    # fall-back date resolves to the first (EDT) occurrence.
    return eastern_to_utc(item.halt_date, item.halt_time, fold=0)


def resume_instant(item: HaltItem) -> datetime | None:
    when = item.resumption_trade_time or item.resumption_quote_time
    if when is None:
        return None
    return eastern_to_utc(item.resumption_date or item.halt_date, when, fold=0)


def prior_pauses(items: Sequence[HaltItem]) -> dict[str, int]:
    """For each volatility pause, how many the same symbol already had that day.

    Derived from the feed itself, which lists the whole session -- no database
    needed. A thin stock can be paused thirty times in a day; only the first is
    information.
    """
    out: dict[str, int] = {}
    ordered = sorted(
        (i for i in items if i.reason in VOLATILITY_REASONS),
        key=lambda i: (i.symbol, i.halt_date, i.halt_time),
    )
    counts: dict[tuple[str, object], int] = {}
    for item in ordered:
        bucket = (item.symbol, item.halt_date)
        out[item.natural_key] = counts.get(bucket, 0)
        counts[bucket] = counts.get(bucket, 0) + 1
    return out


class HaltsAdapter:
    name = "halts"
    # Not gated to the session: T1 news halts land pre-market too, and that is
    # when they matter most.
    market_hours_only = False

    # The feed declares <ttl>1</ttl> -- cache for a minute -- and sits behind a
    # CDN that intermittently answers with a bot challenge instead of the feed.
    # Polling every ten seconds drew a challenge on roughly 8% of requests. Thirty
    # seconds is still fast for a halt, and being polite is what keeps the feed.
    def __init__(self, *, interval: float = 30.0) -> None:
        self.interval = interval

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        payload = await ctx.http.get_bytes(FEED_URL, priority=Priority.HIGH)

        if not looks_like_feed(payload):
            # A 200 with an HTML body: the CDN's JavaScript bot challenge. It is
            # not solved or worked around -- this poll is skipped and the next one
            # almost always gets the feed.
            raise TransientSourceError("halts: CDN returned a challenge page, not the feed")

        digest = hash(payload)
        if ctx.state.get("digest") == digest:
            return []
        ctx.state["digest"] = digest

        items = parse_halts(payload)
        pauses = prior_pauses(items)
        tracked: dict[str, str] = ctx.state.setdefault("tracked", {})

        out: list[RawEvent] = []
        for item in items:
            market_wide = item.reason in MARKET_WIDE_REASONS
            # Filter before anything else, or hundreds of volatility pauses a day
            # reach the resolver for companies nobody is watching.
            if not market_wide and ctx.watched_tickers and item.symbol not in ctx.watched_tickers:
                continue
            # Emit only on a transition: new halt, quotes resumed, trading resumed.
            key = _market_key(item) if market_wide else item.natural_key
            if tracked.get(key) == item.state:
                continue
            tracked[key] = item.state
            while len(tracked) > MAX_TRACKED:
                tracked.pop(next(iter(tracked)))

            out.append(
                RawEvent(
                    source=self.name,
                    external_id=f"halt:{key}",
                    url="https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
                    fetched_at=ctx.clock.now(),
                    payload={
                        "item": item,
                        "key": key,
                        "prior_pauses_today": pauses.get(item.natural_key, 0),
                        "market_wide": market_wide,
                    },
                )
            )
        return out

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        item: HaltItem = raw.payload["item"]
        key: str = raw.payload["key"]
        market_wide: bool = raw.payload["market_wide"]

        resumed_at = resume_instant(item)
        resumes_label = _et_label(resumed_at) if resumed_at else None
        headline, detail = describe_halt(item.reason, item.market, resumes_label)
        company_key = CompanyKey() if market_wide else CompanyKey(ticker=item.symbol)

        base_payload = {
            "symbol": item.symbol,
            "issue_name": item.name,
            "market": item.market,
            "reason": item.reason,
            "threshold_price": item.threshold_price,
            "prior_pauses_today": raw.payload["prior_pauses_today"],
            "market_wide": market_wide,
            "halt_key": key,
        }

        halt = NormalizedEvent(
            source=self.name,
            external_id=f"halt:{key}",
            event_type="halt",
            occurred_at=halt_instant(item),
            company_key=company_key,
            summary=headline,
            url=raw.url,
            payload={**base_payload, "headline": headline, "detail": detail},
        )
        if resumed_at is None:
            return [halt]

        resumption = {
            "state": item.state,
            "at": resumed_at.isoformat(),
            "trading": item.resumption_trade_time is not None,
        }
        resume = NormalizedEvent(
            source=self.name,
            external_id=f"resume:{key}",
            event_type="halt_resume",
            occurred_at=resumed_at,
            company_key=company_key,
            summary="Trading resumed" if resumption["trading"] else "Quotes resumed",
            url=raw.url,
            payload={
                **base_payload,
                "resumption": resumption,
                # Carried so the processor can refresh the halt row's own line.
                "halt_detail": detail,
                "headline": "Trading resumed" if resumption["trading"] else "Quotes resumed",
                "detail": f"{(item.market or 'exchange').lower()} · halted "
                f"{_et_label(halt_instant(item))}",
            },
        )
        return [halt, resume]


def _market_key(item: HaltItem) -> str:
    """One row per market-wide event, however many symbols the feed lists."""
    return f"MARKET|{item.halt_date.isoformat()}|{item.halt_time.isoformat()}|{item.reason}"


def _et_label(moment: datetime) -> str:
    from ..clock import EASTERN

    return moment.astimezone(EASTERN).strftime("%H:%M")
