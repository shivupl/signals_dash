"""Recompute stored scores with the current rules.

Scoring constants are the part of this system most likely to be wrong, and the
corpus exists to judge them. But a stored score is the one the rules gave at
ingest, so tuning a constant leaves two years of history describing the old rules
-- and then the report you tune against is measuring the past.

So: a deliberate, explicit pass. Not automatic, because "every event keeps the
score it was given" is a property worth having by default; the ingest path never
rewrites history, and neither does this unless asked.

Everything needed is already on the event. Payloads carry the transaction codes,
the dollar values, the 8-K items, the relationship and the cluster counts, so
rescoring is the same pure function over the same inputs -- no refetching, and a
rescored event is byte-identical to what a fresh ingest would produce today.

``--dry-run`` reports what would change and writes nothing, which is how you ask
"what would this constant cost me" before paying for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import Score
from .pipeline.promote import count_prior_large_sales
from .scoring import score_event
from .scoring.form4 import Form4Facts, describe_form4, score_form4
from .scoring.one44 import Form144Facts, describe_144, score_144
from .scoring.tables import FORM4_SALE_LARGE_THRESHOLD
from .store.base import EventRow

log = logging.getLogger(__name__)


@dataclass
class Changes:
    seen: int = 0
    changed: int = 0
    flags_before: int = 0
    flags_after: int = 0
    #: (source, before, after) -> count, so the report shows what moved where.
    moves: dict[tuple[str, int, int], int] = field(default_factory=dict)

    def note(self, source: str, before: int, after: int) -> None:
        key = (source, before, after)
        self.moves[key] = self.moves.get(key, 0) + 1


def rescore_row(row: EventRow, prior_large_sales: int = 0) -> Score:
    """The score the current rules would give this event.

    Dispatch mirrors ``pipeline.process._score`` exactly. Cluster counts are read
    back from the payload rather than recounted -- the crowd around an event is a
    fact about when it happened, not about today -- but ``prior_large_sales`` is
    passed in, because rows stored before that rule existed never recorded it and
    the window is fully determined by data already in the table.
    """
    payload: dict[str, Any] = dict(row.payload or {})

    if row.source == "edgar_144":
        facts = Form144Facts.from_payload(payload)
        sellers = int(payload.get("cluster_sellers") or 1)
        return score_144(facts, cluster_sellers=sellers)

    if row.source == "edgar_form4":
        form4 = Form4Facts.from_payload(payload)
        insiders = int(payload.get("cluster_insiders") or 1)
        return score_form4(
            form4, cluster_insiders=insiders, prior_large_sales=prior_large_sales
        )

    # 8-K, 13D/G, halts and system all score from the event itself.
    return score_event(_as_normalized(row))


def redescribe(row: EventRow, score: Score) -> tuple[str, str] | None:
    """The headline and detail the current rules would write, when they differ.

    A rescored insider buy that gains a cluster bonus should say so; leaving the
    old sentence beside a new number is how a feed starts lying quietly.
    """
    payload: dict[str, Any] = dict(row.payload or {})
    if row.source == "edgar_144":
        facts = Form144Facts.from_payload(payload)
        return describe_144(
            facts,
            str(payload.get("seller_name") or "an affiliate"),
            str(payload.get("relationship") or ""),
            int(payload.get("cluster_sellers") or 1),
        )
    if row.source == "edgar_form4" and payload.get("is_open_market_purchase"):
        form4 = Form4Facts.from_payload(payload)
        who = (payload.get("insider_names") or ["an insider"])[0]
        return describe_form4(form4, str(who), int(payload.get("cluster_insiders") or 1))
    return None


class _Normalized:
    """The minimum ``score_event`` reads. Avoids resurrecting a NormalizedEvent,
    which would demand a tz-aware occurred_at and a company key this does not need."""

    def __init__(self, row: EventRow) -> None:
        self.source = row.source
        self.event_type = row.event_type
        self.payload = dict(row.payload or {})


def _as_normalized(row: EventRow) -> Any:
    return _Normalized(row)


async def rescore(
    dsn: str, *, threshold: int, dry_run: bool, sources: Sequence[str] = ()
) -> Changes:
    from .store.pg import PgStore

    store = await PgStore.connect(dsn)
    changes = Changes()
    try:
        for row in await store.all_events(sources):
            changes.seen += 1
            before = row.score
            prior = 0
            payload = row.payload or {}
            if (
                row.source == "edgar_form4"
                and not payload.get("is_open_market_purchase")
                and float(payload.get("sale_value") or 0.0) > FORM4_SALE_LARGE_THRESHOLD
                and row.company_id is not None
            ):
                prior = await count_prior_large_sales(
                    store,
                    row.company_id,
                    [str(c) for c in (payload.get("insider_ciks") or [])],
                    row.occurred_at,
                )
            after = rescore_row(row, prior)
            changes.flags_before += 1 if before >= threshold else 0
            changes.flags_after += 1 if after.total >= threshold else 0
            if after.total == before:
                continue
            changes.changed += 1
            changes.note(row.source, before, after.total)
            if dry_run:
                continue
            await store.update_score(row.id, after)
            described = redescribe(row, after)
            if described is not None:
                await store.update_payload(
                    row.source,
                    row.external_id,
                    {"headline": described[0], "detail": described[1]},
                )
        return changes
    finally:
        await store.close()


def report(changes: Changes, threshold: int, dry_run: bool) -> str:
    lines = [
        "",
        f"{'would change' if dry_run else 'changed'}   {changes.changed} of {changes.seen} events",
        f"flags at {threshold}    {changes.flags_before} -> {changes.flags_after}"
        f"  ({changes.flags_after - changes.flags_before:+d})",
    ]
    if changes.moves:
        lines += ["", "largest moves:"]
        biggest = sorted(changes.moves.items(), key=lambda kv: -kv[1])[:12]
        for (source, before, after) in [k for k, _ in biggest]:
            count = changes.moves[(source, before, after)]
            crossed = ""
            if (before >= threshold) != (after >= threshold):
                crossed = " flagged" if after >= threshold else " unflagged"
            lines.append(f"  {source:14} {before:3} -> {after:3}  {count:6}{crossed}")
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    import argparse

    from .config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and write nothing",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=30,
        help="the bar to count flags against in the report (default 30)",
    )
    parser.add_argument("--sources", default="", help="comma-separated, default all")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    settings = Settings.from_env(require_sec_user_agent=False)
    changes = asyncio.run(
        rescore(
            settings.database_url,
            threshold=args.threshold,
            dry_run=args.dry_run,
            sources=[s for s in args.sources.split(",") if s.strip()],
        )
    )
    print(report(changes, args.threshold, args.dry_run))
    return 0
