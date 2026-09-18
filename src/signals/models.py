"""The shapes every other module is built around.

Three event types rather than one, because each pipeline stage knows a different
set of facts and the types should say so: a ``RawEvent`` has bytes off the wire,
a ``NormalizedEvent`` has been parsed but not attributed to a company, and a
``ResolvedEvent`` has both a company and a score. A single mutable class with
optional fields everywhere would make "is this resolved yet?" a runtime question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MAX_SCORE = 100
MIN_SCORE = 0


def normalize_cik(raw: str | int | None) -> str | None:
    """Zero-pad a CIK to the canonical 10 characters.

    The three sources disagree and it is silent when it goes wrong:
    company_tickers.json gives a bare int (320193), Form 4 XML gives it padded
    (0002073340), and data.sec.gov wants the padded form in the URL. If a CIK
    reaches company_alias in the wrong shape, resolution simply misses and the
    feed stays mysteriously empty. Everything funnels through here.
    """
    if raw is None:
        return None
    digits = str(raw).strip().lstrip("0")
    if not digits.isdigit():
        return None
    return digits.zfill(10)


@dataclass(frozen=True, slots=True)
class CompanyKey:
    """Candidate identifiers for resolution, tried strongest first."""

    cik: str | None = None
    ticker: str | None = None
    name: str | None = None

    def candidates(self) -> list[tuple[str, str]]:
        """(kind, value) pairs to look up in company_alias, in priority order."""
        out: list[tuple[str, str]] = []
        if self.cik:
            out.append(("cik", self.cik))
        if self.ticker:
            out.append(("ticker", self.ticker.upper()))
        if self.name:
            out.append(("legal_name", self.name.strip().lower()))
        return out

    def __bool__(self) -> bool:
        return bool(self.cik or self.ticker or self.name)


@dataclass(frozen=True, slots=True)
class RawEvent:
    """What an adapter's fetch() produced, including any hydrated sub-document.

    payload is a plain JSON-safe mapping so a fixture round-trips without codecs.
    """

    source: str
    external_id: str
    url: str | None
    fetched_at: datetime
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """One row's worth of event, parsed but not yet attributed or scored."""

    source: str
    external_id: str
    event_type: str
    occurred_at: datetime
    company_key: CompanyKey
    summary: str
    url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # One assertion at the parser boundary catches most timezone bugs in this
        # project. occurred_at feeds the latency metric; if it is ever naive, the
        # number is wrong by hours and nothing else complains.
        if self.occurred_at.tzinfo is None:
            raise ValueError(
                f"{self.source}/{self.external_id}: occurred_at is naive; "
                "parse to tz-aware UTC before constructing a NormalizedEvent"
            )


@dataclass(frozen=True, slots=True)
class Score:
    """A total plus its itemized reasons.

    ``parts`` is not decoration. It is what makes retroactive cluster promotion
    idempotent -- promotion asks "does this row already carry a cluster part?"
    rather than trying to infer it from the total -- and it is what the UI shows
    when you click a flag and ask why it is an 82.
    """

    total: int
    parts: dict[str, int] = field(default_factory=dict)
    needs_model: bool = False

    @classmethod
    def of(cls, parts: dict[str, int], *, needs_model: bool = False) -> Score:
        raw = sum(parts.values())
        total = max(MIN_SCORE, min(MAX_SCORE, raw))
        recorded = dict(parts)
        if total != raw:
            # Keep "the parts explain the total" true. Without this the UI shows
            # a breakdown summing to 110 next to a score of 100, and the one
            # question the breakdown exists to answer gets a wrong answer.
            recorded["clamped"] = total - raw
        return cls(total=total, parts=recorded, needs_model=needs_model)

    @classmethod
    def zero(cls) -> Score:
        return cls(total=0, parts={})


@dataclass(frozen=True, slots=True)
class ResolvedEvent:
    """A normalized event with a company (possibly None) and a score."""

    normalized: NormalizedEvent
    company_id: int | None
    score: Score

    @property
    def source(self) -> str:
        return self.normalized.source

    @property
    def external_id(self) -> str:
        return self.normalized.external_id

    @property
    def occurred_at(self) -> datetime:
        return self.normalized.occurred_at


@dataclass(frozen=True, slots=True)
class Company:
    id: int
    cik: str | None
    ticker: str | None
    name: str
    watched: bool
