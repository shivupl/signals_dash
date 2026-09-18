"""The severity classifier seam. Phase 1 ships the null implementation.

Rules do the work. A model is reserved for the one judgement rules genuinely
cannot make -- how bad a given 8-K actually is inside its item band -- and even
then only below the rule-only floor, because a model able to talk a restatement
down from 95 is a liability.

This exists now, unused, because retrofitting an async call into a synchronous
scoring path later is a refactor, whereas leaving a no-op behind an interface
costs one line at the call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ClassifyRequest:
    event_type: str
    items: Sequence[str]
    title: str
    text: str
    rule_score: int


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    #: None means "no opinion, keep the rule score". The stub is therefore a
    #: no-op by construction rather than by a caller-side feature flag.
    score: int | None
    rationale: str
    confidence: float
    model: str


class SeverityClassifier(Protocol):
    async def classify(self, request: ClassifyRequest) -> ClassifyResult: ...


class NullClassifier:
    """Phase 1. Always defers to the rules."""

    model = "null-stub"

    async def classify(self, request: ClassifyRequest) -> ClassifyResult:
        return ClassifyResult(
            score=None,
            rationale="rules-only",
            confidence=1.0,
            model=self.model,
        )
