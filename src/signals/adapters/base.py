"""The adapter contract.

``fetch`` owns all IO, including any secondary enrichment request. That keeps
``normalize`` synchronous and pure, so every normalization test is a plain
function call against a fixture with no event loop and no network in sight.

``normalize`` returns a *sequence*, not one event: a Form 4 carrying only option
grants yields nothing, and a halt seen for the first time already resumed yields
both the halt and its resume.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..clock import Clock
from ..http import SourceClient
from ..models import NormalizedEvent, RawEvent


@dataclass
class FetchContext:
    http: SourceClient
    clock: Clock
    #: CIKs worth spending a document fetch on. Hydrating every Form 4 on EDGAR
    #: would be thousands of requests a day, and after 16:00 ET a burst of them.
    watched_ciks: frozenset[str] = frozenset()
    #: Every share class of every watched company. Empty means "no pre-filter".
    watched_tickers: frozenset[str] = frozenset()
    #: Per-adapter scratch that survives iterations -- high-water marks, pending
    #: groups. Handed in by the runner rather than held on the adapter, so a test
    #: can pre-seed it and assert on it afterwards.
    state: MutableMapping[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    name: str
    #: Seconds between iterations.
    interval: float
    #: True only for sources that are meaningless outside the session. Filings
    #: are not: EDGAR accepts until roughly 22:00 ET and the heaviest 8-K window
    #: is just after the close.
    market_hours_only: bool

    async def fetch(self, ctx: FetchContext) -> Sequence[RawEvent]: ...

    def normalize(self, raw: RawEvent) -> Sequence[NormalizedEvent]: ...
