"""One event's journey: resolve, filter, score, store, publish."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from ..bus.base import BusMessage, Publisher
from ..models import NormalizedEvent, ResolvedEvent, Score
from ..prices import PriceService
from ..resolve.resolver import Resolver
from ..scoring import score_event
from ..scoring.form4 import Form4Facts, describe_form4, score_form4
from ..store.base import Store
from .promote import Promoter, count_cluster_insiders

log = logging.getLogger(__name__)


@dataclass
class ProcessStats:
    seen: int = 0
    resolved: int = 0
    stored: int = 0
    duplicate: int = 0
    published: int = 0
    skipped_unwatched: int = 0
    promoted: int = 0

    def __iadd__(self, other: ProcessStats) -> ProcessStats:
        self.seen += other.seen
        self.resolved += other.resolved
        self.stored += other.stored
        self.duplicate += other.duplicate
        self.published += other.published
        self.skipped_unwatched += other.skipped_unwatched
        self.promoted += other.promoted
        return self


class Processor:
    def __init__(
        self,
        store: Store,
        resolver: Resolver,
        publisher: Publisher,
        *,
        flag_threshold: int,
        watched_only: bool = True,
        started_at: datetime | None = None,
        prices: PriceService | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._publisher = publisher
        self._threshold = flag_threshold
        self._watched_only = watched_only
        self._prices = prices
        self._background: set[asyncio.Task[None]] = set()
        self._promoter = Promoter(store, publisher, flag_threshold=flag_threshold)
        #: When this worker began watching. A filing accepted before that was
        #: already in the index when we arrived, so its "detection lag" measures
        #: the age of the backlog, not our speed -- and must not reach /stats.
        self._started_at = started_at

    async def process(self, event: NormalizedEvent) -> ProcessStats:
        stats = ProcessStats(seen=1)
        # A market-wide circuit breaker is about no company in particular. It is
        # stored with no company and shown to everyone, so it skips resolution
        # and the watchlist filter entirely.
        market_wide = bool(event.payload.get("market_wide"))
        company = None if market_wide else await self._resolver.resolve(event)
        if company is not None:
            stats.resolved = 1

        # EDGAR publishes thousands of filings a day from companies nobody here
        # is watching. Dropping them before the insert is what keeps the database
        # small enough to stay fast on one small VPS.
        if not market_wide and self._watched_only and (company is None or not company.watched):
            stats.skipped_unwatched = 1
            return stats

        # An insider buy is scored with the cluster already around it, so a
        # filing that completes a cluster is correct the moment it is stored --
        # promotion then only has to reach backwards, never forwards.
        cluster_insiders = 1
        if company is not None and _is_insider_buy(event):
            cluster_insiders = await count_cluster_insiders(
                self._store, company.id, event.occurred_at
            )

        live = self._started_at is not None and event.occurred_at >= self._started_at
        marked = NormalizedEvent(
            source=event.source,
            external_id=event.external_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            company_key=event.company_key,
            summary=event.summary,
            url=event.url,
            payload={**event.payload, "live_capture": live},
        )
        resolved = ResolvedEvent(
            normalized=marked,
            company_id=company.id if company else None,
            score=_score(marked, cluster_insiders),
        )

        if event.event_type == "halt_resume":
            # Runs whether or not the resume row is new: quotes can resume before
            # trading does, and the second transition arrives as a "duplicate".
            await self._annotate_halt(event)

        event_id = await self._store.insert_event(resolved)
        if event_id is None:
            # Already stored. The same filing is seen on every poll for as long
            # as it stays in the window, so this is the common case, not an error.
            stats.duplicate = 1
            return stats
        stats.stored = 1

        if resolved.score.total >= self._threshold:
            row = await self._store.get_event(event_id)
            if row is not None:
                await self._publisher.publish(BusMessage(type="event.new", data=_serialize(row)))
                stats.published = 1
            # After the publish, never before: the flag is already on screen by
            # the time a quote is even requested, so a slow or dead price source
            # costs a blank number and nothing else.
            if self._prices is not None and company is not None and company.ticker:
                task = asyncio.create_task(self._stamp_price(event_id, company.ticker))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

        # Reach backwards: earlier buys from other insiders were scored before
        # this one existed, and a 10b5-1 buy sitting at 10 becomes a 35 here.
        if company is not None and _is_insider_buy(event):
            promotion = await self._promoter.promote(company.id, event.occurred_at)
            stats.promoted = len(promotion.promoted)
        return stats

    async def _stamp_price(self, event_id: int, ticker: str) -> None:
        """Record the price when the flag fired. It cannot be reconstructed later."""
        assert self._prices is not None
        try:
            price = await self._prices.quote(ticker)
            if price is None:
                return
            await self._store.stamp_price_at(event_id, price)
            row = await self._store.get_event(event_id)
            if row is not None:
                await self._publisher.publish(
                    BusMessage(type="event.updated", data=_serialize(row))
                )
        except Exception:  # noqa: BLE001 -- annotation must never surface as a failure
            log.exception("price stamp failed event=%s ticker=%s", event_id, ticker)

    async def drain(self) -> None:
        """Wait for in-flight stamps. For tests and for a clean shutdown."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def _annotate_halt(self, resume: NormalizedEvent) -> None:
        """Write the resumption onto the halt's own row, and say so once.

        So the feed can show "halted 10:32, resumes 10:47" on one row. Each merge
        is guarded, making repeats true no-ops that publish nothing.
        """
        key = resume.payload.get("halt_key")
        resumption = resume.payload.get("resumption") or {}
        if not key:
            return
        halt_id = await self._store.merge_payload(
            "halts",
            f"halt:{key}",
            {"resumption": resumption, "detail": resume.payload.get("halt_detail")},
            "resumption",
        )
        if resumption.get("trading"):
            # Second transition: quotes resumed earlier, trading resumes now.
            traded = await self._store.merge_payload(
                "halts",
                f"halt:{key}",
                {
                    "resumed_trading_at": resumption.get("at"),
                    "detail": resume.payload.get("halt_detail"),
                },
                "resumed_trading_at",
            )
            halt_id = halt_id or traded
        if halt_id is not None:
            row = await self._store.get_event(halt_id)
            if row is not None and row.score >= self._threshold:
                await self._publisher.publish(
                    BusMessage(type="event.updated", data=_serialize(row))
                )


def _is_insider_buy(event: NormalizedEvent) -> bool:
    return event.source == "edgar_form4" and event.event_type == "form4_buy"


def _score(event: NormalizedEvent, cluster_insiders: int) -> Score:
    """Form 4 needs the cluster count; everything else scores from itself."""
    if _is_insider_buy(event):
        facts = Form4Facts.from_payload(event.payload)
        score = score_form4(facts, cluster_insiders=cluster_insiders)
        who = (event.payload.get("insider_names") or ["an insider"])[0]
        headline, detail = describe_form4(facts, who, cluster_insiders)
        event.payload["headline"] = headline
        event.payload["detail"] = detail
        event.payload["cluster_insiders"] = cluster_insiders
        return score
    return score_event(event)


def _serialize(row: object) -> dict[str, object]:
    """Full object, never a patch -- a client filtering by score may not hold
    this event at all and has to be able to insert it."""
    from ..api.schemas import EventOut

    return EventOut.of(row).model_dump(mode="json")  # type: ignore[arg-type]
