"""Poll EDGAR's current-filings index.

This is the latency path. ``<updated>`` on an atom entry is the acceptance
timestamp -- the moment the market could have known -- and the gap between that
and our insert is the only number worth quoting.

**Store first, parse second.** The index entry is inserted as soon as it is seen.
Anything requiring a second request (Form 4's ownership XML) happens afterwards,
in a later step. If a parser has a bug the filing is still recorded, and latency
is measured from the index hit, which is genuinely when we knew.

Form 4 resolution deserves a note. A filing appears once per party: the reporting
insider *and* the issuer, and a joint filing can produce eleven entries for one
accession. The CIK in a ``(Reporting)`` title belongs to the insider, who will
never be on a watchlist, so the company is taken only from the entry whose role
is ``(Issuer)``. When the window truncates mid-group and no issuer entry is
present, the filing is stored unresolved rather than attributed to a guess.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Sequence
from typing import Final

from ..models import CompanyKey, NormalizedEvent, RawEvent
from ..parsers.edgar_atom import AccessionGroup, group_by_accession, parse_atom
from ..parsers.edgar_titles import Role
from ..ratelimit import Priority
from ..scoring.eightk import describe_8k, summarize_8k
from ..scoring.thirteen_dg import describe_13dg, summarize_13dg
from .base import Adapter, FetchContext

# browse-edgar's `type` parameter is a PREFIX match, not an exact one. Asking for
# type=4 returns 424B2 prospectus supplements, 424B3 and 485APOS alongside actual
# Form 4s -- in a live sample, only 38 of 100 entries were really Form 4. Asking
# for 8-K also returns 8-K/A. So every adapter declares the base forms it accepts
# and the rest are discarded after parsing, before anything is stored.
INDEX_URL: Final[str] = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type={form}&count=100&output=atom"
)


def index_url(form: str) -> str:
    """13D and 13G need separate queries, and the space must be encoded."""
    return INDEX_URL.format(form=urllib.parse.quote(form))


# --- normalization (pure; shared by the live adapter and the fixture replay) ---


def normalize_8k(group: AccessionGroup) -> NormalizedEvent:
    entry = group.primary
    name = entry.title.name if entry.title else None
    headline, detail = describe_8k(group.items)
    return NormalizedEvent(
        source="edgar_8k",
        external_id=group.accession,
        event_type="8k",
        occurred_at=group.updated,
        company_key=CompanyKey(cik=group.filer_cik or entry.cik, name=name),
        summary=summarize_8k(group.items),
        url=group.link,
        payload={
            "accession": group.accession,
            "items": list(group.items),
            "filer_name": name,
            "form": entry.title.form if entry.title else "8-K",
            "headline": headline,
            "detail": detail,
        },
    )


def normalize_13dg(group: AccessionGroup) -> NormalizedEvent:
    entry = group.primary
    form = entry.title.form if entry.title else "SC 13D"
    name = entry.title.name if entry.title else None
    # The (Subject) party is the company being reported on; (Filer) is the
    # investor doing the reporting.
    subject = next((e for e in group.entries if e.role is Role.SUBJECT), None)
    headline, detail = describe_13dg(form, name)
    return NormalizedEvent(
        source="edgar_13dg",
        external_id=group.accession,
        event_type="13d" if "13D" in form.upper() else "13g",
        occurred_at=group.updated,
        company_key=CompanyKey(
            cik=(subject.cik if subject else group.filer_cik),
            name=(subject.title.name if subject and subject.title else None),
        ),
        summary=summarize_13dg(form, name),
        url=group.link,
        payload={
            "accession": group.accession,
            "form": form,
            "filer_name": name,
            "headline": headline,
            "detail": detail,
        },
    )


def normalize_form4(group: AccessionGroup) -> NormalizedEvent:
    """Index-only. The transaction code lives in the ownership XML, so nothing
    here can say whether this was a purchase; the event scores zero until the
    hydration step exists."""
    issuer = next((e for e in group.entries if e.role is Role.ISSUER), None)
    issuer_name = issuer.title.name if issuer and issuer.title else None
    insiders = [e.title.name for e in group.entries if e.role is Role.REPORTING and e.title]
    who = insiders[0] if len(insiders) == 1 else f"{len(insiders)} insiders"
    return NormalizedEvent(
        source="edgar_form4",
        external_id=group.accession,
        event_type="form4_index",
        occurred_at=group.updated,
        # Deliberately narrow: only the issuer entry names the company. A
        # reporting owner's CIK belongs to a person.
        company_key=CompanyKey(cik=group.issuer_hint, name=issuer_name),
        summary=f"Form 4 filed by {who}",
        url=group.link,
        payload={
            "accession": group.accession,
            "issuer_name": issuer_name,
            "insider_names": insiders,
            "insider_ciks": list(group.reporting_ciks),
            "has_issuer_entry": group.has_issuer,
            "headline": "Insider transaction reported",
            "detail": f"form 4 · edgar · {who}",
        },
    )


NORMALIZERS = {
    "edgar_8k": normalize_8k,
    "edgar_13dg": normalize_13dg,
    "edgar_form4": normalize_form4,
}


class EdgarIndexAdapter:
    """One adapter per logical source, each polling one or more form types."""

    market_hours_only = False

    def __init__(
        self,
        name: str,
        forms: Sequence[str],
        *,
        accepts: Sequence[str] | None = None,
        interval: float = 2.0,
    ) -> None:
        self.name = name
        self.forms = tuple(forms)
        #: Base form types this adapter will keep. Defaults to exactly what it
        #: asked for, which is the safe reading of a prefix-matching API.
        self.accepts = frozenset(accepts or forms)
        self.interval = interval

    def _wanted(self, group: AccessionGroup) -> bool:
        title = group.primary.title
        if title is None:
            return False
        return title.base_form.upper() in {f.upper() for f in self.accepts}

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        out: list[RawEvent] = []
        seen: set[str] = set()
        for form in self.forms:
            payload = await ctx.http.get_bytes(index_url(form), priority=Priority.HIGH)

            # The feed is re-served unchanged most of the time. Hashing it skips
            # the parse, which is CPU rather than requests -- the request was
            # already spent.
            digest = hash(payload)
            if ctx.state.get(f"digest:{form}") == digest:
                continue
            ctx.state[f"digest:{form}"] = digest

            for group in group_by_accession(parse_atom(payload)):
                if group.accession in seen or not self._wanted(group):
                    continue
                seen.add(group.accession)
                out.append(
                    RawEvent(
                        source=self.name,
                        external_id=group.accession,
                        url=group.link,
                        fetched_at=ctx.clock.now(),
                        payload={"group": group},
                    )
                )
        return out

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        group = raw.payload["group"]
        normalizer = NORMALIZERS[self.name]
        return [normalizer(group)]


def build_edgar_adapters(interval: float = 2.0) -> list[Adapter]:
    """The three EDGAR sources.

    Separate adapters rather than one, so a parser bug in Form 4 cannot stop 8-Ks
    arriving -- the runner isolates failures per adapter.
    """
    from .edgar_form4 import EdgarForm4Adapter

    return [
        EdgarIndexAdapter("edgar_8k", ["8-K"], accepts=["8-K"], interval=interval),
        # Form 4 needs the ownership document, so it has its own adapter.
        EdgarForm4Adapter(interval=interval),
        EdgarIndexAdapter(
            "edgar_13dg",
            ["SC 13D", "SC 13G"],
            accepts=["SC 13D", "SC 13G"],
            interval=interval * 2,
        ),
    ]
