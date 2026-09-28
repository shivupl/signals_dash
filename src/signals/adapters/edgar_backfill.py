"""Reconcile against SEC's own per-company filing records.

The index poll is fast but lossy in one specific way: ``getcurrent`` is a sliding
window of 100 entries, and ``type=4`` is mostly prospectus noise, so at peak the
window holds only a few minutes of real Form 4s. A filing accepted while the
worker is down, restarting, or stuck behind a slow response scrolls out and is
gone -- two of fifteen were lost that way during one afternoon of restarts.

``data.sec.gov/submissions/CIK##########.json`` does not slide. It is the
authoritative list of what each company has filed, it is static JSON that answers
in about 0.1 s, and the watchlist is forty requests. So on startup and every half
hour this walks the watchlist and feeds anything missing through the normal
pipeline. The accession number is the dedupe key, so whatever the index poll
already caught is a no-op.

This is the safety net, not the fast path. Latency still comes from the index.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from ..models import NormalizedEvent, RawEvent
from ..parsers.edgar_atom import AccessionGroup, AtomEntry
from ..parsers.edgar_titles import ParsedTitle, Role
from ..parsers.form4_xml import parse_form4
from ..parsers.form144_xml import parse_form144
from ..ratelimit import Priority
from .base import FetchContext
from .edgar_144 import Edgar144Adapter
from .edgar_form4 import EdgarForm4Adapter
from .edgar_index import normalize_8k, normalize_13dg

log = logging.getLogger(__name__)

SUBMISSIONS_URL: Final[str] = "https://data.sec.gov/submissions/CIK{cik}.json"
LOOKBACK: Final[timedelta] = timedelta(days=3)
MAX_SEEN: Final[int] = 20000

_8K = {"8-K"}
_FORM4 = {"4"}
_13DG = {"SC 13D", "SC 13G"}
_144 = {"144"}


def _base(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


def recent_filings(payload: bytes, since: datetime) -> list[dict[str, Any]]:
    """Wanted filings accepted after ``since``, from one submissions document."""
    body = json.loads(payload)
    return _filings(
        body.get("filings", {}).get("recent", {}),
        since,
        cik=str(body.get("cik", "")).zfill(10),
        name=body.get("name") or "",
    )


def shard_names(payload: bytes, since: datetime) -> list[str]:
    """Older submission shards that could still hold filings after ``since``.

    ``filings.recent`` caps at 1,000 filings, which for most companies reaches back
    years -- but a heavy filer can exhaust it inside a couple of months, and then
    the history simply stops without saying so. The shard index carries each file's
    date range, so only the ones that overlap the window are fetched.
    """
    body = json.loads(payload)
    out: list[str] = []
    for shard in body.get("filings", {}).get("files", []):
        name = shard.get("name")
        to = shard.get("filingTo") or ""
        if not name:
            continue
        # filingTo is a filing date, not an acceptance timestamp; comparing dates
        # keeps a shard that straddles the cutoff rather than dropping it.
        if not to or to >= since.date().isoformat():
            out.append(str(name))
    return out


def shard_filings(
    payload: bytes, since: datetime, *, cik: str, name: str
) -> list[dict[str, Any]]:
    """The same records from an older shard, which is a flat document with no
    ``filings`` wrapper and no company identity of its own."""
    return _filings(json.loads(payload), since, cik=cik, name=name)


def _filings(
    rows: dict[str, Any], since: datetime, *, cik: str, name: str
) -> list[dict[str, Any]]:
    recent = rows
    out: list[dict[str, Any]] = []
    for form, accession, accepted, items in zip(
        recent.get("form", []),
        recent.get("accessionNumber", []),
        recent.get("acceptanceDateTime", []),
        recent.get("items", []),
        strict=False,
    ):
        if _base(form) not in _8K | _FORM4 | _13DG | _144:
            continue
        try:
            # Documented as UTC, e.g. "2026-09-18T00:53:51.000Z".
            when = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when < since:
            continue
        out.append(
            {
                "form": form,
                "accession": accession,
                "accepted": when,
                "items": [i.strip() for i in (items or "").split(",") if i.strip()],
                "cik": cik,
                "name": name,
            }
        )
    return out


def as_group(filing: dict[str, Any]) -> AccessionGroup:
    """Dress a submissions record as an index group, so one set of normalizers
    serves both paths and the two cannot drift."""
    form = filing["form"]
    base = _base(form)
    role = (
        Role.ISSUER
        if base in _FORM4
        else Role.SUBJECT
        if base in _13DG | _144
        else Role.FILER
    )
    folder = filing["accession"].replace("-", "")
    link = (
        f"https://www.sec.gov/Archives/edgar/data/{int(filing['cik'])}/{folder}/"
        f"{filing['accession']}-index.htm"
    )
    entry = AtomEntry(
        accession=filing["accession"],
        title=ParsedTitle(form=form, name=filing["name"], cik=filing["cik"], role=role),
        updated=filing["accepted"],
        link=link,
        summary="",
        items=tuple(filing["items"]),
    )
    return AccessionGroup(
        accession=filing["accession"],
        entries=(entry,),
        updated=filing["accepted"],
        items=tuple(filing["items"]),
    )


class EdgarBackfillAdapter:
    name = "edgar_backfill"
    market_hours_only = False

    def __init__(self, *, interval: float = 1800.0, slices: int = 4) -> None:
        self.interval = interval
        #: How many sweeps one rotation of the non-core universe takes. Four
        #: half-hourly sweeps is two hours, well inside the 3-day lookback.
        self.slices = max(1, slices)
        self._form4 = EdgarForm4Adapter()
        self._one44 = Edgar144Adapter()

    def sweep_ciks(self, ctx: FetchContext) -> list[str]:
        """Core every sweep, the rest one slice at a time.

        Pure and tested on its own, because the sweep's cost is the thing most
        likely to surprise whoever widens the watchlist next.
        """
        if not ctx.core_ciks:
            return sorted(ctx.watched_ciks)
        core = sorted(ctx.core_ciks & ctx.watched_ciks)
        rest = sorted(ctx.watched_ciks - ctx.core_ciks)
        turn = int(ctx.state.get("slice", 0)) % self.slices
        ctx.state["slice"] = (turn + 1) % self.slices
        return core + rest[turn :: self.slices]

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        since = ctx.clock.now() - LOOKBACK
        out: list[RawEvent] = []

        for cik in self.sweep_ciks(ctx):
            try:
                payload = await ctx.http.get_bytes(
                    SUBMISSIONS_URL.format(cik=cik), priority=Priority.LOW
                )
                filings = recent_filings(payload, since)
            except Exception as exc:  # noqa: BLE001 -- one company must not stop the sweep
                log.warning("backfill: %s failed: %s", cik, exc)
                continue
            out.extend(await self.from_filings(ctx, filings))

        if out:
            log.info("backfill: %d filings to reconcile", len(out))
        return out

    async def from_filings(
        self, ctx: FetchContext, filings: Sequence[dict[str, Any]]
    ) -> list[RawEvent]:
        """Hydrate and dress already-listed filings as raw events.

        Split out from ``fetch`` so the historical backfill can supply filings from
        a longer window, and from older submission shards, without a second copy of
        the hydration and dedupe rules.
        """
        seen: dict[str, bool] = ctx.state.setdefault("seen", {})
        out: list[RawEvent] = []
        for filing in filings:
            if filing["accession"] in seen:
                continue
            seen[filing["accession"]] = True
            while len(seen) > MAX_SEEN:
                seen.pop(next(iter(seen)))

            group = as_group(filing)
            base = _base(filing["form"])
            payload_out: dict[str, Any] = {"group": group, "base": base}
            # Form 4 and Form 144 both carry their signal in the document, not the
            # index record: the transaction code for one, the amount and the
            # relationship for the other.
            if base in _FORM4 | _144:
                doc = await self._hydrate(ctx, group, base)
                if doc is None:
                    seen.pop(filing["accession"], None)  # try again next sweep
                    continue
                payload_out["doc"] = doc
            out.append(
                RawEvent(
                    source=self.name,
                    external_id=filing["accession"],
                    url=group.link,
                    fetched_at=ctx.clock.now(),
                    payload=payload_out,
                )
            )
        return out

    async def _hydrate(self, ctx: FetchContext, group: AccessionGroup, base: str) -> Any:
        assert group.link is not None
        url = f"{group.link.rsplit('/', 1)[0]}/{group.accession}.txt"
        parse = parse_form144 if base in _144 else parse_form4
        try:
            return parse(await ctx.http.get_bytes(url, priority=Priority.LOW))
        except Exception as exc:  # noqa: BLE001 -- one filing is not the sweep
            log.warning("backfill: %s %s not hydrated: %s", base, group.accession, exc)
            return None

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]:
        """Emit under the ORIGINAL source names, so an event found here dedupes
        against -- and is indistinguishable from -- one found by the index poll."""
        group: AccessionGroup = raw.payload["group"]
        base: str = raw.payload["base"]
        if base in _8K:
            return [normalize_8k(group)]
        if base in _13DG:
            return [normalize_13dg(group)]
        if base in _144:
            return self._one44.normalize(raw)
        return self._form4.normalize(raw)
