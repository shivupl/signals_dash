"""Parse the EDGAR ``getcurrent`` atom feed.

Two things here are load-bearing.

**Timestamps.** ``<updated>`` is the acceptance time, carrying a real offset
(``2026-09-15T21:57:13-04:00``). That is "the moment the market could have known",
so it becomes ``occurred_at`` and one half of the latency metric. The ``Filed:``
date in the summary is a calendar date only and is not a substitute.

**Grouping.** One filing produces one atom entry *per party*. A Form 4 usually
yields two -- the reporting insider and the issuer -- but a joint filing yields
many: a real capture from this feed had a single accession spread across eleven
entries, ten reporting owners and one issuer. Since all of them share an
accession number and the accession is the natural dedupe key, entries are grouped
before anything downstream looks at them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from lxml import etree

from .edgar_titles import ParsedTitle, Role, parse_title

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

_ACCNO_IN_ID = re.compile(r"accession-number=([0-9-]+)")
_ACCNO_IN_SUMMARY = re.compile(r"AccNo:\s*</b>\s*([0-9-]+)", re.IGNORECASE)
# The 8-K item numbers are carried in the summary itself, e.g.
#   <br>Item 5.02: Departure of Directors...
# which means item-based scoring needs no second request at all.
_ITEM = re.compile(r"Item\s+(\d{1,2}\.\d{2})")


@dataclass(frozen=True, slots=True)
class AtomEntry:
    accession: str
    title: ParsedTitle | None
    updated: datetime
    link: str | None
    summary: str
    items: tuple[str, ...] = ()

    @property
    def role(self) -> Role:
        return self.title.role if self.title else Role.UNKNOWN

    @property
    def cik(self) -> str | None:
        return self.title.cik if self.title else None


@dataclass(frozen=True, slots=True)
class AccessionGroup:
    """Every atom entry that belongs to one filing.

    ``issuer_cik`` is a *hint* only. For a Form 4 the authoritative issuer comes
    from the ownership XML; trusting the feed here is how a filing gets attributed
    to the insider instead of the company.
    """

    accession: str
    entries: tuple[AtomEntry, ...]
    updated: datetime
    items: tuple[str, ...] = ()

    @property
    def issuer_hint(self) -> str | None:
        for entry in self.entries:
            if entry.role is Role.ISSUER:
                return entry.cik
        return None

    @property
    def reporting_ciks(self) -> tuple[str, ...]:
        return tuple(
            e.cik for e in self.entries if e.role is Role.REPORTING and e.cik is not None
        )

    @property
    def filer_cik(self) -> str | None:
        for entry in self.entries:
            if entry.role is Role.FILER:
                return entry.cik
        return None

    @property
    def has_issuer(self) -> bool:
        return self.issuer_hint is not None

    @property
    def primary(self) -> AtomEntry:
        """The entry to describe the filing with: issuer if present, else first."""
        for entry in self.entries:
            if entry.role is Role.ISSUER:
                return entry
        return self.entries[0]

    @property
    def link(self) -> str | None:
        return self.primary.link


def parse_atom(payload: bytes) -> list[AtomEntry]:
    """Parse feed bytes into entries. An empty feed is normal, not an error."""
    root = etree.fromstring(payload)  # noqa: S320 -- SEC feed, no external entities
    entries: list[AtomEntry] = []
    for node in root.findall("a:entry", ATOM_NS):
        summary = _text(node, "a:summary")
        accession = _accession(node, summary)
        if accession is None:
            continue
        updated = _updated(node)
        if updated is None:
            continue
        entries.append(
            AtomEntry(
                accession=accession,
                title=parse_title(_text(node, "a:title")),
                updated=updated,
                link=_link(node),
                summary=summary,
                items=tuple(dict.fromkeys(_ITEM.findall(summary))),
            )
        )
    return entries


def group_by_accession(entries: list[AtomEntry]) -> list[AccessionGroup]:
    """Collapse per-party entries into one group per filing, newest first.

    This is what removes the Reporting/Issuer race inside a single response: the
    group carries every party at once, so which entry the parser happens to reach
    first stops mattering.
    """
    buckets: dict[str, list[AtomEntry]] = {}
    for entry in entries:
        buckets.setdefault(entry.accession, []).append(entry)

    groups = [
        AccessionGroup(
            accession=accession,
            entries=tuple(group),
            updated=max(e.updated for e in group),
            items=tuple(dict.fromkeys(i for e in group for i in e.items)),
        )
        for accession, group in buckets.items()
    ]
    groups.sort(key=lambda g: g.updated, reverse=True)
    return groups


def _text(node: etree._Element, path: str) -> str:
    found = node.find(path, ATOM_NS)
    return (found.text or "") if found is not None else ""


def _link(node: etree._Element) -> str | None:
    found = node.find("a:link", ATOM_NS)
    return found.get("href") if found is not None else None


def _accession(node: etree._Element, summary: str) -> str | None:
    raw_id = _text(node, "a:id")
    match = _ACCNO_IN_ID.search(raw_id) or _ACCNO_IN_SUMMARY.search(summary)
    return match.group(1) if match else None


def _updated(node: etree._Element) -> datetime | None:
    raw = _text(node, "a:updated").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # An offset-less timestamp here would silently corrupt every latency number,
    # so it is dropped rather than guessed at.
    return parsed if parsed.tzinfo is not None else None
