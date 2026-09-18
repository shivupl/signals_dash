"""Scoring dispatch: a normalized event in, a Score out.

One 0-100 scale across every source, so the feed can sort a trading halt against
an insider buy. Sources without a scorer yet return zero rather than a guess --
the event is still stored and still visible at min_score=0, it simply makes no
claim about importance it cannot back up.
"""

from __future__ import annotations

from ..models import NormalizedEvent, Score
from .eightk import score_8k
from .halts import score_halt, score_resume
from .thirteen_dg import score_13dg

__all__ = ["score_event", "score_8k", "score_13dg"]


def score_event(event: NormalizedEvent) -> Score:
    if event.source == "edgar_8k":
        return score_8k(event.payload.get("items", []))
    if event.source == "edgar_13dg":
        return score_13dg(str(event.payload.get("form", "")))
    if event.source == "system":
        # The pipeline reporting on itself; severity is set by whoever raised it.
        return Score.of({"system": int(event.payload.get("severity") or 50)})
    if event.source == "halts":
        if event.event_type == "halt_resume":
            return score_resume()
        return score_halt(
            str(event.payload.get("reason", "")),
            prior_pauses_today=int(event.payload.get("prior_pauses_today") or 0),
        )
    # edgar_form4 lands here until the Form 4 scorer exists: the transaction code
    # is the whole signal and it lives in the ownership XML, which the index-only
    # adapter has not fetched.
    return Score.zero()
