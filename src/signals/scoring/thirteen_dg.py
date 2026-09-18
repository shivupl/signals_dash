"""Score a Schedule 13D or 13G.

No parsing needed: the form type is the signal. 13D means the filer intends to
influence the company; 13G means they are passive and merely large.
"""

from __future__ import annotations

from ..models import Score
from .tables import (
    SCHEDULE_13D,
    SCHEDULE_13D_AMENDED,
    SCHEDULE_13G,
    SCHEDULE_13G_AMENDED,
)


def score_13dg(form: str) -> Score:
    normalized = form.strip().upper().replace("SC ", "")
    amended = normalized.endswith("/A")
    base = normalized[:-2] if amended else normalized

    if base == "13D":
        value = SCHEDULE_13D_AMENDED if amended else SCHEDULE_13D
        key = "13d_amended" if amended else "13d_activist"
    elif base == "13G":
        value = SCHEDULE_13G_AMENDED if amended else SCHEDULE_13G
        key = "13g_amended" if amended else "13g_passive"
    else:
        return Score.zero()
    return Score.of({key: value})


def describe_13dg(form: str, filer: str | None = None) -> tuple[str, str]:
    normalized = form.strip().upper().replace("SC ", "")
    amended = normalized.endswith("/A")
    base = normalized[:-2] if amended else normalized

    if base == "13D":
        headline = "Activist stake amended" if amended else "Activist stake disclosed"
    elif base == "13G":
        headline = "Passive stake amended" if amended else "Passive stake disclosed"
    else:
        headline = f"{form} filed"

    detail = f"{normalized.lower()} · edgar"
    if filer:
        detail += f" · {filer}"
    return headline, detail


def summarize_13dg(form: str, filer: str | None = None) -> str:
    normalized = form.strip().upper().replace("SC ", "")
    amended = normalized.endswith("/A")
    base = normalized[:-2] if amended else normalized
    who = f" by {filer}" if filer else ""
    if base == "13D":
        what = "Activist stake amended" if amended else "Activist stake disclosed"
    elif base == "13G":
        what = "Passive stake amended" if amended else "Passive stake disclosed"
    else:
        return f"{form} filed{who}"
    return f"{what}{who}"
