"""Retroactive cluster promotion.

Three distinct insiders buying the same company inside thirty days is a different
signal from one insider buying three times, and the third filing is what turns
the earlier two into evidence. So when it lands, the earlier ones are rescored.

That is not cosmetic. A 10b5-1 purchase scores 40 - 30 = 10 and is stored without
being flagged; add the cluster bonus and it becomes 35, which crosses the
threshold. An event that was correctly ignored an hour ago becomes worth reading,
and nothing else in the pipeline can make that happen.

Two invariants hold this together.

**Idempotence.** Promotion only touches rows whose ``score_parts`` carry no
``cluster`` key, so running it twice changes nothing and costs nothing.

**Publish after commit.** The score is written and committed before any message
goes out. Publishing inside the transaction lets a fast client fetch the event
and read the pre-promotion score.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..bus.base import BusMessage, Publisher
from ..models import Score
from ..scoring.form4 import Form4Facts, score_form4
from ..scoring.tables import (
    FORM4_CLUSTER_MIN_INSIDERS,
    FORM4_CLUSTER_WINDOW_DAYS,
    FORM4_SALE_LARGE_THRESHOLD,
    FORM4_SALE_REPEAT_WINDOW_DAYS,
    FORM144_CLUSTER_WINDOW_DAYS,
)
from ..store.base import EventRow, Store

log = logging.getLogger(__name__)

CLUSTER_WINDOW = timedelta(days=FORM4_CLUSTER_WINDOW_DAYS)


@dataclass
class PromotionResult:
    cluster_insiders: int = 0
    promoted: list[EventRow] = field(default_factory=list)
    newly_flagged: list[int] = field(default_factory=list)

    @property
    def is_cluster(self) -> bool:
        return self.cluster_insiders >= FORM4_CLUSTER_MIN_INSIDERS


async def count_cluster_insiders(store: Store, company_id: int, now: datetime) -> int:
    """Distinct insiders with an open-market buy inside the window.

    By CIK, never by name: "John A. Smith" and "SMITH JOHN A" are one person, and
    string matching would split them and invent a cluster out of one buyer.
    """
    return await store.distinct_p_buyers(company_id, now - CLUSTER_WINDOW)


async def count_cluster_sellers(
    store: Store, company_id: int, now: datetime, sellers: tuple[str, ...] = ()
) -> int:
    """Distinct insiders noticing a sale inside the window, this notice included.

    By CIK, for the same reason as the buy side: filing agents spell one person's
    name several ways, and a name match would invent a crowd out of one seller.

    ``sellers`` is the notice being scored. It has to be counted here because
    notices are never rescored afterwards -- the buy side can count only what is
    stored, since promotion reaches back and fixes the arithmetic later, but a 144
    gets one chance to see the crowd it is part of.
    """
    return await store.distinct_144_sellers(
        company_id, now - timedelta(days=FORM144_CLUSTER_WINDOW_DAYS), sellers
    )


async def count_prior_large_sales(
    store: Store, company_id: int, insider_ciks: Sequence[str], occurred_at: datetime
) -> int:
    """Large sales this insider already filed in the window before this one.

    Shared by the ingest path and the rescore pass on purpose: they must agree, or
    the corpus stops being a baseline and becomes a second opinion. Strictly before
    ``occurred_at``, so the first sale of a programme scores full and the rest are
    damped.
    """
    return await store.prior_large_sales(
        company_id,
        insider_ciks,
        occurred_at - timedelta(days=FORM4_SALE_REPEAT_WINDOW_DAYS),
        occurred_at,
        FORM4_SALE_LARGE_THRESHOLD,
    )


def rescore_with_cluster(row: EventRow, cluster_insiders: int) -> Score:
    """The same pure function used at ingest, so a promoted score is identical to
    what a fresh ingest would have produced."""
    return score_form4(Form4Facts.from_payload(row.payload), cluster_insiders=cluster_insiders)


class Promoter:
    def __init__(self, store: Store, publisher: Publisher, *, flag_threshold: int) -> None:
        self._store = store
        self._publisher = publisher
        self._threshold = flag_threshold

    def set_flag_threshold(self, value: int) -> None:
        self._threshold = value

    async def promote(self, company_id: int, now: datetime) -> PromotionResult:
        """Rescore earlier buys once a company's cluster is complete."""
        result = PromotionResult()
        result.cluster_insiders = await count_cluster_insiders(self._store, company_id, now)
        if not result.is_cluster:
            return result

        candidates = await self._store.events_missing_cluster_bonus(
            company_id, now - CLUSTER_WINDOW
        )
        if not candidates:
            return result

        # Write everything first; announce nothing until it is committed.
        for row in candidates:
            score = rescore_with_cluster(row, result.cluster_insiders)
            await self._store.update_score(row.id, score)
            result.promoted.append(row)
            if row.score < self._threshold <= score.total:
                result.newly_flagged.append(row.id)

        await self._publish(result)
        return result

    async def _publish(self, result: PromotionResult) -> None:
        """Announce the change after it is durable.

        The message carries the whole event, never a patch. A client filtering at
        min_score never received the sub-threshold version, so it has to be able
        to insert from the update rather than apply a delta to something it does
        not hold.
        """
        from ..api.schemas import EventOut

        for row in result.promoted:
            fresh = await self._store.get_event(row.id)
            if fresh is None:
                continue
            await self._publisher.publish(
                BusMessage(type="event.updated", data=EventOut.of(fresh).model_dump(mode="json"))
            )
        if result.promoted:
            log.info(
                "cluster promotion company=%s insiders=%d rescored=%d newly_flagged=%d",
                result.promoted[0].company_id,
                result.cluster_insiders,
                len(result.promoted),
                len(result.newly_flagged),
            )

