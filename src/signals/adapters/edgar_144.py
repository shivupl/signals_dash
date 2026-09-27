"""Form 144, with document hydration.

A Form 144 is filed *before* an affiliate sells restricted stock, which makes it
the earliest public notice of insider selling -- days ahead of the Form 4 that
reports the same sale after the fact.

The shape is Form 4's, with one difference that decides everything: **the company
is the ``(Subject)`` entry, not ``(Issuer)``**. A ``(Reporting)`` CIK belongs to
the person selling, who will never be on a watchlist, so resolving from it would
drop every notice on the floor. The document is authoritative either way.

The rest mirrors Form 4 deliberately, because the failure modes are the same:

* hydrate only when the subject is watched -- about eighteen notices arrive a day
  market-wide and only a handful concern a watched company;
* a missing subject entry means wait, not guess, because the 100-entry window
  truncates mid-filing and the unique constraint would lock a wrong attribution
  in permanently;
* after the grace period, fetch anyway on the low-priority lane, so a genuinely
  split filing is not lost.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from ..models import CompanyKey, NormalizedEvent, RawEvent
from ..parsers.edgar_atom import AccessionGroup, group_by_accession, parse_atom
from ..parsers.edgar_titles import Role
from ..parsers.form144_xml import Form144Doc, Form144ParseError, parse_form144
from ..ratelimit import Priority
from ..scoring.one44 import Form144Facts, describe_144
from .base import FetchContext
from .edgar_form4 import submission_url
from .edgar_index import INDEX_HEDGE_AFTER, index_url

log = logging.getLogger(__name__)

#: How long to wait for the subject entry before fetching the document to find
#: out. Entries for one filing share an acceptance time and sit adjacent in the
#: feed, so the sibling almost always arrives on the very next poll.
SUBJECT_GRACE_SECONDS: Final[float] = 60.0

MAX_PENDING: Final[int] = 2000
MAX_HYDRATED: Final[int] = 5000


@dataclass
class Form144Counters:
    hydrated: int = 0
    skipped_unwatched: int = 0
    waiting_for_subject: int = 0
    grace_expired: int = 0
    parse_failures: int = 0
    fetch_failures: int = 0


@dataclass
class Form144State:
    """Per-adapter scratch. Mirrors Form 4's, kept separate because the counters
    and the log line differ and neither adapter should be able to disturb the
    other's bookkeeping."""

    hydrated: dict[str, bool] = field(default_factory=dict)
    pending: dict[str, float] = field(default_factory=dict)
    counters: Form144Counters = field(default_factory=Form144Counters)

    def remember(self, accession: str) -> None:
        self.hydrated[accession] = True
        while len(self.hydrated) > MAX_HYDRATED:
            self.hydrated.pop(next(iter(self.hydrated)))

    def wait(self, accession: str, now: float) -> float:
        """Seconds this accession has been waiting for its subject entry."""
        first = self.pending.setdefault(accession, now)
        while len(self.pending) > MAX_PENDING:
            self.pending.pop(next(iter(self.pending)))
        return now - first

    def done_waiting(self, accession: str) -> None:
        self.pending.pop(accession, None)


def subject_hint(group: AccessionGroup) -> str | None:
    """The company's CIK, from the (Subject) entry. 13D/G uses the same role."""
    for entry in group.entries:
        if entry.role is Role.SUBJECT:
            return entry.cik
    return None


class Edgar144Adapter:
    name = "edgar_144"
    market_hours_only = False

    def __init__(self, *, interval: float = 4.0, grace: float = SUBJECT_GRACE_SECONDS) -> None:
        self.interval = interval
        self.grace = grace
        #: Declared, because browse-edgar's `type` is a prefix match.
        self.accepts = frozenset({"144"})

    def _state(self, ctx: FetchContext) -> Form144State:
        state = ctx.state.get("form144")
        if not isinstance(state, Form144State):
            state = Form144State()
            ctx.state["form144"] = state
        return state

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        state = self._state(ctx)
        payload = await ctx.http.get_bytes(
            index_url("144"), priority=Priority.HIGH, hedge_after=INDEX_HEDGE_AFTER
        )

        digest = hash(payload)
        if ctx.state.get("digest:144") == digest:
            return []
        ctx.state["digest:144"] = digest

        out: list[RawEvent] = []
        now = ctx.clock.monotonic()

        for group in group_by_accession(parse_atom(payload)):
            title = group.primary.title
            if title is None or title.base_form.upper() != "144":
                continue
            if group.accession in state.hydrated:
                continue

            document = await self._maybe_hydrate(group, ctx, state, now)
            if document is None:
                continue

            state.remember(group.accession)
            state.done_waiting(group.accession)
            out.append(
                RawEvent(
                    source=self.name,
                    external_id=group.accession,
                    url=group.link,
                    fetched_at=ctx.clock.now(),
                    payload={"group": group, "doc": document},
                )
            )

        if out:
            c = state.counters
            log.info(
                "form144 hydrated=%d skipped_unwatched=%d waiting=%d grace_expired=%d "
                "parse_fail=%d fetch_fail=%d",
                c.hydrated,
                c.skipped_unwatched,
                c.waiting_for_subject,
                c.grace_expired,
                c.parse_failures,
                c.fetch_failures,
            )
        return out

    async def _maybe_hydrate(
        self, group: AccessionGroup, ctx: FetchContext, state: Form144State, now: float
    ) -> Form144Doc | None:
        counters = state.counters
        hint = subject_hint(group)

        if hint is not None:
            if hint not in ctx.watched_ciks:
                counters.skipped_unwatched += 1
                state.remember(group.accession)
                return None
        else:
            waited = state.wait(group.accession, now)
            if waited < self.grace:
                counters.waiting_for_subject += 1
                return None
            counters.grace_expired += 1

        url = submission_url(group.link, group.accession)
        if url is None:
            return None

        try:
            # Low lane: document fetches must never delay an index poll, because
            # the index hit is where latency is measured.
            body = await ctx.http.get_bytes(url, priority=Priority.LOW)
        except Exception as exc:  # noqa: BLE001 -- one bad filing is not an outage
            counters.fetch_failures += 1
            log.warning("form144 fetch failed accession=%s: %s", group.accession, exc)
            return None

        try:
            document = parse_form144(body)
        except Form144ParseError as exc:
            counters.parse_failures += 1
            log.warning("form144 parse failed accession=%s: %s", group.accession, exc)
            return None

        if hint is None and (document.issuer_cik or "") not in ctx.watched_ciks:
            # Resolved late, and not ours after all.
            counters.skipped_unwatched += 1
            state.remember(group.accession)
            return None

        counters.hydrated += 1
        return document

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        group: AccessionGroup = raw.payload["group"]
        doc: Form144Doc = raw.payload["doc"]

        facts = Form144Facts(
            value=doc.value or 0.0,
            percent_outstanding=doc.percent_outstanding,
            is_insider=doc.is_insider,
            plan_10b5_1=doc.plan_10b5_1,
            prior_sales=doc.prior_sales,
        )
        who = doc.seller_name or "an affiliate"
        headline, detail = describe_144(facts, who, doc.relationship)

        return [
            NormalizedEvent(
                source=self.name,
                external_id=group.accession,
                event_type="sale_notice",
                occurred_at=group.updated,
                # The document first; the index's (Subject) CIK only if a filing
                # omits its own issuer, which no sampled filing did.
                company_key=CompanyKey(
                    cik=doc.issuer_cik or subject_hint(group), name=doc.issuer_name
                ),
                summary=headline,
                url=group.link,
                payload={
                    "accession": group.accession,
                    "issuer_cik": doc.issuer_cik,
                    "issuer_name": doc.issuer_name,
                    "seller_name": doc.seller_name,
                    # Cluster detection counts distinct sellers by CIK: a name is
                    # spelled differently by different filing agents.
                    "seller_ciks": list(group.reporting_ciks),
                    "relationship": doc.relationship,
                    "is_insider": doc.is_insider,
                    "security_class": doc.security_class,
                    "units": doc.units,
                    "value": doc.value,
                    "units_outstanding": doc.units_outstanding,
                    "percent_outstanding": doc.percent_outstanding,
                    "approx_sale_date": doc.approx_sale_date,
                    "exchange": doc.exchange,
                    "broker": doc.broker,
                    "plan_10b5_1": doc.plan_10b5_1,
                    "plan_dates": list(doc.plan_dates),
                    "prior_sales": doc.prior_sales,
                    "prior_sales_value": doc.prior_sales_value,
                    "headline": headline,
                    "detail": detail,
                },
            )
        ]
