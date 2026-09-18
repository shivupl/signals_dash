"""Score an 8-K from its item numbers.

The item numbers are the whole game, and they arrive free: the atom index summary
already lists them, so scoring happens the moment the filing is seen, with no
second request and nothing added to the latency path.

A filing usually carries several items. The most serious one decides -- an 8-K
announcing both a restatement and an exhibit list is a restatement.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Score
from .tables import DEFAULT_ITEM_BAND, ITEM_BANDS, ITEM_LABELS, RULE_ONLY_FLOOR


def score_8k(items: Sequence[str]) -> Score:
    """Return the score for a set of 8-K item numbers.

    ``needs_model`` is decided here rather than at the call site, so the
    "85 and above is rule-only" guarantee cannot be forgotten by a future caller.
    """
    if not items:
        # An 8-K with no parsed items is still a filing worth recording, but
        # nothing about it is known yet.
        low, high = DEFAULT_ITEM_BAND
        return Score.of({"8k_unknown_item": high}, needs_model=False)

    known = [i for i in items if i in ITEM_BANDS]
    if not known:
        low, high = DEFAULT_ITEM_BAND
        return Score.of({"8k_routine": high}, needs_model=False)

    # Rank by ceiling, then floor: the most serious item is the one that matters.
    worst = max(known, key=lambda i: ITEM_BANDS[i])
    low, high = ITEM_BANDS[worst]

    parts = {f"8k_item_{worst.replace('.', '_')}": low}
    # Below the rule-only floor a classifier may later lift the score toward the
    # ceiling; until then the floor is the honest answer.
    needs_model = low < RULE_ONLY_FLOOR and high > low
    return Score.of(parts, needs_model=needs_model)


def describe_8k(items: Sequence[str]) -> tuple[str, str]:
    """Split the description in two: what happened, and where it came from.

    The feed reads better when the headline carries meaning -- "Prior financials
    should not be relied on" -- and the item number and source sit underneath as
    provenance. Jamming both into one line buries the part you actually scan.
    """
    known = [i for i in items if i in ITEM_BANDS]
    if not known:
        extra = f"item {', '.join(items)} · edgar" if items else "edgar"
        return "8-K filed", extra

    worst = max(known, key=lambda i: ITEM_BANDS[i])
    headline = ITEM_LABELS.get(worst, "Material event")
    others = len([i for i in items if i != worst])
    detail = f"8-K item {worst} · edgar"
    if others:
        detail += f" · +{others} more item{'s' if others > 1 else ''}"
    return headline, detail


def summarize_8k(items: Sequence[str]) -> str:
    """One line for the feed, built from the items alone."""
    known = [i for i in items if i in ITEM_BANDS]
    if not known:
        if items:
            return f"8-K — item {', '.join(items)}"
        return "8-K filed"
    worst = max(known, key=lambda i: ITEM_BANDS[i])
    label = ITEM_LABELS.get(worst, "Material event")
    extra = len([i for i in items if i != worst])
    suffix = f" (+{extra} more)" if extra else ""
    return f"8-K Item {worst} — {label}{suffix}"
