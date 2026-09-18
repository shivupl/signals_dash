"""Score a trading halt.

The reason code is the signal. T1 -- news pending -- is the one that matters
most: the exchange has stopped the stock because something material lands in
minutes. At the other end, a limit-up/limit-down pause is mechanical, and a thin
stock can trip dozens in one session.
"""

from __future__ import annotations

from ..models import Score
from .tables import (
    HALT_DEFAULT_SCORE,
    HALT_REASON_LABELS,
    HALT_REASON_SCORES,
    HALT_REPEAT_PAUSE_SCORE,
    HALT_RESUME_SCORE,
    VOLATILITY_REASONS,
)


def score_halt(reason: str, *, prior_pauses_today: int = 0) -> Score:
    code = reason.strip().upper()
    if code in VOLATILITY_REASONS and prior_pauses_today > 0:
        # The first pause of the day is information. The rest are the same fact
        # repeating, and unflagged they stay on the timeline without the noise.
        return Score.of({"halt_repeat_pause": HALT_REPEAT_PAUSE_SCORE})
    value = HALT_REASON_SCORES.get(code, HALT_DEFAULT_SCORE)
    return Score.of({f"halt_{code.lower() or 'unknown'}": value})


def score_resume() -> Score:
    return Score.of({"halt_resume": HALT_RESUME_SCORE})


def describe_halt(reason: str, market: str | None, resumes: str | None) -> tuple[str, str]:
    code = reason.strip().upper()
    headline = HALT_REASON_LABELS.get(code, f"Trading halted ({code or 'no reason given'})")
    bits = [(market or "exchange").lower(), f"code {code}" if code else "halt"]
    if resumes:
        bits.append(f"resumes {resumes}")
    return headline, " · ".join(bits)
