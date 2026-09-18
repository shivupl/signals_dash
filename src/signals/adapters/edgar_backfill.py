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
from ..parsers.form4_xml import Form4ParseError, parse_form4
from ..ratelimit import Priority
from .base import FetchContext
from .edgar_form4 import EdgarForm4Adapter
from .edgar_index import normalize_8k, normalize_13dg

log = logging.getLogger(__name__)

SUBMISSIONS_URL: Final[str] = "https://data.sec.gov/submissions/CIK{cik}.json"
LOOKBACK: Final[timedelta] = timedelta(days=3)
MAX_SEEN: Final[int] = 20000

_8K = {"8-K"}
_FORM4 = {"4"}
_13DG = {"SC 13D", "SC 13G"}


def _base(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


def recent_filings(payload: bytes, since: datetime) -> list[dict[str, Any]]:
    """Wanted filings accepted after ``since``, from one submissions document."""
    body = json.loads(payload)
    recent = body.get("filings", {}).get("recent", {})
    cik = str(body.get("cik", "")).zfill(10)
    name = body.get("name") or ""
    out: list[dict[str, Any]] = []
    for form, accession, accepted, items in zip(
        recent.get("form", []),
        recent.get("accessionNumber", []),
        recent.get("acceptanceDateTime", []),
        recent.get("items", []),
        strict=False,
    ):
        if _base(form) not in _8K | _FORM4 | _13DG:
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
    role = Role.ISSUER if base in _FORM4 else Role.SUBJECT if base in _13DG else Role.FILER
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

    def __init__(self, *, interval: float = 1800.0) -> None:
        self.interval = interval
        self._form4 = EdgarForm4Adapter()

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]:
        seen: dict[str, bool] = ctx.state.setdefault("seen", {})
        since = ctx.clock.now() - LOOKBACK
        out: list[RawEvent] = []

        for cik in sorted(ctx.watched_ciks):
            try:
                payload = await ctx.http.get_bytes(
                    SUBMISSIONS_URL.format(cik=cik), priority=Priority.LOW
                )
                filings = recent_filings(payload, since)
            except Exception as exc:  # noqa: BLE001 -- one company must not stop the sweep
                log.warning("backfill: %s failed: %s", cik, exc)
                continue

            for filing in filings:
                if filing["accession"] in seen:
                    continue
                seen[filing["accession"]] = True
                while len(seen) > MAX_SEEN:
                    seen.pop(next(iter(seen)))

                group = as_group(filing)
                payload_out: dict[str, Any] = {"group": group, "base": _base(filing["form"])}
                if payload_out["base"] in _FORM4:
                    doc = await self._hydrate(ctx, group)
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
        if out:
            log.info("backfill: %d filings to reconcile", len(out))
        return out

    async def _hydrate(self, ctx: FetchContext, group: AccessionGroup) -> Any:
        assert group.link is not None
        url = f"{group.link.rsplit('/', 1)[0]}/{group.accession}.txt"
        try:
            return parse_form4(await ctx.http.get_bytes(url, priority=Priority.LOW))
        except (Form4ParseError, Exception) as exc:  # noqa: BLE001
            log.warning("backfill: form 4 %s not hydrated: %s", group.accession, exc)
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
        return self._form4.normalize(raw)
