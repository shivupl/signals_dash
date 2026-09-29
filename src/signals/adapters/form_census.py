"""Watch for form types nobody claims.

Three bugs in this project have been the same bug: our code matching text that
somebody else controls and can change without telling us.

  * asking EDGAR for ``type=4`` also returned 424B2 prospectuses, because `type`
    is a prefix match -- Form 4 resolution sat at 42% until that was found;
  * a "President" rule matched "Vice President", inflating senior-officer bonuses;
  * ``SC 13G`` became ``SCHEDULE 13G`` in 2025, and the 13D/G source went dead for
    nine months while answering every poll.

The third is the dangerous shape, because the query returned *nothing* -- so the
new spelling never appeared anywhere the system was looking. No amount of care in
the 13D/G adapter could have caught it; it was looking through a window that had
stopped showing the thing.

This looks through a different window. The index's ``getcurrent`` feed with no
``type`` filter is every form arriving right now, about a hundred at a time, one
request. Normalise each form name to its family -- strip the ``SC``/``SCHEDULE``
prefix and any ``/A`` -- and any family we already claim, arriving under a spelling
we do not, is a rename in progress.

One request every fifteen minutes, and it would have turned nine months into a day.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Final

from ..models import CompanyKey, NormalizedEvent, RawEvent
from ..parsers.edgar_atom import parse_atom
from ..ratelimit import Priority
from .base import FetchContext

log = logging.getLogger(__name__)

CENSUS_URL: Final[str] = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&count=100&output=atom"
)

#: Strip the prefix EDGAR puts in front of a schedule. Both spellings, because the
#: whole point is that both exist.
_PREFIX: Final[re.Pattern[str]] = re.compile(r"^\s*(?:SCHEDULE|SC)\s+", re.I)


def family(form: str) -> str:
    """The form stripped to what it is, ignoring how it is spelled today.

    ``SC 13G`` and ``SCHEDULE 13G`` are both ``13G``; ``4/A`` is ``4``. Amendments
    collapse into the base form because an adapter that wants 13D wants 13D/A too.
    """
    bare = _PREFIX.sub("", form.strip().upper())
    return bare[:-2].strip() if bare.endswith("/A") else bare


def unclaimed_spellings(seen: Iterable[str], claimed: Iterable[str]) -> dict[str, set[str]]:
    """Families we claim, arriving under spellings we do not ask for.

    Returns family -> the unexpected spellings. An empty result is the normal state
    and the only one that should ever be quiet.

    Forms from families we do not claim are ignored: the census sees the entire
    firehose -- S-1s, N-PORTs, X-17A-5s -- and complaining about those would bury
    the one line that matters.
    """
    claimed_literals = {f.strip().upper() for f in claimed}
    claimed_families = {family(f) for f in claimed_literals}

    out: dict[str, set[str]] = {}
    for form in seen:
        literal = form.strip().upper()
        if not literal:
            continue
        # An amendment of something we claim is claimed.
        base = literal[:-2].strip() if literal.endswith("/A") else literal
        if base in claimed_literals:
            continue
        kin = family(literal)
        if kin in claimed_families:
            out.setdefault(kin, set()).add(base)
    return out


class FormCensusAdapter:
    """Polls the unfiltered index and reports form names nobody claims."""

    name = "form_census"
    market_hours_only = False

    def __init__(self, claimed: Sequence[str] = (), *, interval: float = 900.0) -> None:
        self.interval = interval
        self.claimed = tuple(claimed)

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        payload = await ctx.http.get_bytes(CENSUS_URL, priority=Priority.LOW)
        forms = [e.title.form for e in parse_atom(payload) if e.title]
        surprises = unclaimed_spellings(forms, self.claimed)

        already: dict[str, bool] = ctx.state.setdefault("reported", {})
        out: list[RawEvent] = []
        for kin, spellings in sorted(surprises.items()):
            for spelling in sorted(spellings):
                key = f"{kin}|{spelling}"
                if key in already:
                    continue
                already[key] = True
                log.error(
                    "form census: %r is a %s and no adapter asks for it -- renamed?",
                    spelling,
                    kin,
                )
                out.append(
                    RawEvent(
                        source=self.name,
                        external_id=f"unclaimed:{key}",
                        url=None,
                        fetched_at=ctx.clock.now(),
                        payload={"family": kin, "spelling": spelling},
                    )
                )
        if not out:
            log.debug("form census: %d forms seen, all accounted for", len(set(forms)))
        return out

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        spelling = raw.payload["spelling"]
        kin = raw.payload["family"]
        return [
            NormalizedEvent(
                source="system",
                external_id=raw.external_id,
                event_type="form_unclaimed",
                occurred_at=raw.fetched_at,
                company_key=CompanyKey(),
                summary=f"{spelling} is being filed and no adapter asks for it",
                url=None,
                payload={
                    "market_wide": True,
                    "adapter": "form_census",
                    "state": "open",
                    # High: this is the shape of failure that made 13D/G look
                    # healthy for nine months.
                    "severity": 80,
                    "family": kin,
                    "spelling": spelling,
                    "headline": f"{spelling} is being filed and nothing claims it",
                    "detail": (
                        f"system · we ask for the {kin} family under other names · "
                        "SEC renames forms without notice"
                    ),
                },
            )
        ]
