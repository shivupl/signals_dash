"""Form 4, with document hydration.

The index says a Form 4 was filed. It does not say what happened: the transaction
code -- the entire signal -- lives in the ownership document, so a second request
is unavoidable for the filings that matter.

Three things govern when that request is spent.

**Only the issuer names the company.** A filing appears once per party, and a
``(Reporting)`` entry's CIK belongs to the insider, who is a person and will
never be on a watchlist. Resolution comes from the ``(Issuer)`` entry, and the
authoritative answer from the document itself.

**A missing issuer entry means wait, not guess.** The 100-entry window truncates
mid-filing. Rather than attributing the filing to an insider -- which the unique
constraint would then lock in permanently -- the accession is held briefly and
reconsidered on the next poll, when its sibling entries have usually arrived.

**Grace expiry is the backstop.** If an accession is still issuer-less after the
grace period, the document is fetched anyway on the low-priority lane and the
issuer read from it, so a genuinely split filing is not lost.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from ..models import CompanyKey, NormalizedEvent, RawEvent
from ..parsers.edgar_atom import AccessionGroup, group_by_accession, parse_atom
from ..parsers.form4_xml import Form4Doc, Form4ParseError, parse_form4
from ..ratelimit import Priority
from .base import FetchContext
from .edgar_index import INDEX_HEDGE_AFTER, index_url

log = logging.getLogger(__name__)

#: How long to wait for an accession's issuer entry before fetching the document
#: to find out. Entries for one filing share an acceptance time and are adjacent
#: in the feed, so the sibling almost always arrives on the very next poll.
ISSUER_GRACE_SECONDS: Final[float] = 60.0

#: Caps on the per-adapter scratch dicts, so a long run cannot grow them without
#: bound.
MAX_PENDING: Final[int] = 2000
MAX_HYDRATED: Final[int] = 5000


@dataclass
class Form4Counters:
    """Operational counters worth watching.

    ``hint_mismatch`` should be zero forever: if the index says one issuer and
    the document says another, the title parser has a bug.
    """

    hydrated: int = 0
    skipped_unwatched: int = 0
    waiting_for_issuer: int = 0
    grace_expired: int = 0
    hint_mismatch: int = 0
    parse_failures: int = 0
    fetch_failures: int = 0


@dataclass
class Form4State:
    pending: dict[str, float] = field(default_factory=dict)
    hydrated: dict[str, bool] = field(default_factory=dict)
    counters: Form4Counters = field(default_factory=Form4Counters)

    def remember(self, accession: str) -> None:
        self.hydrated[accession] = True
        _trim(self.hydrated, MAX_HYDRATED)

    def wait(self, accession: str, now: float) -> float:
        first = self.pending.setdefault(accession, now)
        _trim(self.pending, MAX_PENDING)
        return now - first

    def done_waiting(self, accession: str) -> None:
        self.pending.pop(accession, None)


def _trim(store: dict[str, object] | dict[str, float] | dict[str, bool], limit: int) -> None:
    """Drop the oldest entries. Insertion order is age order here."""
    while len(store) > limit:
        store.pop(next(iter(store)))


def submission_url(index_link: str | None, accession: str) -> str | None:
    """The complete submission text, which carries the ownership XML inline.

    One request rather than two. The XML document's filename is not predictable
    -- live filings use ownership.xml, rdgdoc.xml and wk-form4_*.xml -- so
    fetching it by name would need a directory listing first.
    """
    if not index_link:
        return None
    directory = index_link.rsplit("/", 1)[0]
    return f"{directory}/{accession}.txt"


class EdgarForm4Adapter:
    name = "edgar_form4"
    market_hours_only = False

    def __init__(self, *, interval: float = 2.0, grace: float = ISSUER_GRACE_SECONDS) -> None:
        self.interval = interval
        self.grace = grace
        self.accepts = frozenset({"4"})

    def _state(self, ctx: FetchContext) -> Form4State:
        state = ctx.state.get("form4")
        if not isinstance(state, Form4State):
            state = Form4State()
            ctx.state["form4"] = state
        return state

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        state = self._state(ctx)
        payload = await ctx.http.get_bytes(
            index_url("4"), priority=Priority.HIGH, hedge_after=INDEX_HEDGE_AFTER
        )

        digest = hash(payload)
        if ctx.state.get("digest:4") == digest:
            return []
        ctx.state["digest:4"] = digest

        out: list[RawEvent] = []
        now = ctx.clock.monotonic()

        for group in group_by_accession(parse_atom(payload)):
            # browse-edgar's `type` is a prefix match: type=4 also returns 424B2,
            # 424B3 and 485APOS.
            title = group.primary.title
            if title is None or title.base_form.upper() != "4":
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
                "form4 hydrated=%d skipped_unwatched=%d waiting=%d grace_expired=%d "
                "hint_mismatch=%d parse_fail=%d fetch_fail=%d",
                c.hydrated,
                c.skipped_unwatched,
                c.waiting_for_issuer,
                c.grace_expired,
                c.hint_mismatch,
                c.parse_failures,
                c.fetch_failures,
            )
        return out

    async def _maybe_hydrate(
        self, group: AccessionGroup, ctx: FetchContext, state: Form4State, now: float
    ) -> Form4Doc | None:
        counters = state.counters

        if group.has_issuer:
            if group.issuer_hint not in ctx.watched_ciks:
                # Nothing about an unwatched company's insider is worth a request.
                counters.skipped_unwatched += 1
                state.remember(group.accession)
                return None
        else:
            waited = state.wait(group.accession, now)
            if waited < self.grace:
                # The sibling entries almost always arrive on the next poll.
                counters.waiting_for_issuer += 1
                return None
            # Genuinely split. Find out from the document rather than lose it.
            counters.grace_expired += 1

        url = submission_url(group.link, group.accession)
        if url is None:
            return None

        try:
            # Low lane: a burst of document fetches must never delay an index
            # poll, because the index hit is where latency is measured.
            body = await ctx.http.get_bytes(url, priority=Priority.LOW)
        except Exception as exc:  # noqa: BLE001 -- one bad filing is not an outage
            counters.fetch_failures += 1
            log.warning("form4 fetch failed accession=%s: %s", group.accession, exc)
            return None

        try:
            document = parse_form4(body)
        except Form4ParseError as exc:
            counters.parse_failures += 1
            log.warning("form4 parse failed accession=%s: %s", group.accession, exc)
            return None

        if group.issuer_hint and document.issuer_cik != group.issuer_hint:
            # The document wins. A non-zero count here means the title parser is
            # wrong, not that EDGAR is.
            counters.hint_mismatch += 1
            log.warning(
                "form4 issuer mismatch accession=%s index=%s document=%s",
                group.accession,
                group.issuer_hint,
                document.issuer_cik,
            )
        counters.hydrated += 1
        return document

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        group: AccessionGroup = raw.payload["group"]
        doc: Form4Doc = raw.payload["doc"]

        owner = doc.primary_owner
        who = owner.name if owner else "an insider"
        role = owner.role if owner else "Insider"

        if doc.is_open_market_purchase:
            headline = f"Open-market buy by {who}"
            detail_bits = [f"form 4 · {role.lower()}"]
            if doc.purchase_value:
                detail_bits.append(f"${doc.purchase_value:,.0f}")
            if doc.plan_10b5_1:
                # Pre-scheduled, so it signals nothing about what they think now.
                detail_bits.append("10b5-1 plan")
            detail = " · ".join(detail_bits)
            event_type = "form4_buy"
        else:
            codes = sorted({t.code for t in doc.transactions})
            headline = f"Form 4 filed by {who}"
            detail = f"form 4 · {role.lower()} · {', '.join(codes) or 'no transactions'}"
            event_type = "form4_other"

        return [
            NormalizedEvent(
                source=self.name,
                external_id=group.accession,
                event_type=event_type,
                occurred_at=group.updated,
                # From the document, never from the index title.
                company_key=CompanyKey(cik=doc.issuer_cik, name=doc.issuer_name),
                summary=headline,
                url=group.link,
                payload={
                    "accession": group.accession,
                    "issuer_cik": doc.issuer_cik,
                    "issuer_name": doc.issuer_name,
                    "issuer_symbol": doc.issuer_symbol,
                    # Cluster detection counts distinct insiders by CIK.
                    "insider_ciks": list(doc.owner_ciks),
                    "insider_names": [o.name for o in doc.owners],
                    "role": role,
                    "is_officer": bool(owner and owner.is_officer),
                    "is_director": bool(owner and owner.is_director),
                    "officer_title": owner.officer_title if owner else None,
                    "codes": sorted({t.code for t in doc.transactions}),
                    "is_open_market_purchase": doc.is_open_market_purchase,
                    "purchase_shares": doc.purchase_shares,
                    "purchase_value": doc.purchase_value,
                    "shares_after": doc.shares_after,
                    "plan_10b5_1": doc.plan_10b5_1,
                    "headline": headline,
                    "detail": detail,
                },
            )
        ]
