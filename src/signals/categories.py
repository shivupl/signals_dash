"""One vocabulary for "what kind of event is this", across every source.

Sources name things their own way -- an 8-K has item numbers, a Form 4 has
transaction codes, a halt has a reason code. The feed's event-type filter needs a
single small set that means the same thing everywhere, and this is the only place
that mapping is written down.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from .scoring.tables import ITEM_BANDS

CATEGORY_LABELS: Final[dict[str, str]] = {
    "insider_buy": "Insider buy",
    "insider_sell": "Insider sell",
    "grant_award": "Grant / award",
    "officer_change": "Officer / director change",
    "material_agreement": "Material agreement",
    "earnings": "Earnings",
    # Restatement, bankruptcy, delisting, auditor change. Not in the original
    # list, but these are the highest-scoring events in the system and "other" is
    # the wrong place to have to look for them.
    "distress": "Restatement / distress",
    "halt": "Trading halt",
    "activist_stake": "Activist stake",
    "other": "Other",
    "system": "System",
}

_8K_ITEM_CATEGORY: Final[dict[str, str]] = {
    "5.02": "officer_change",
    "1.01": "material_agreement",
    "2.02": "earnings",
    "4.02": "distress",
    "1.03": "distress",
    "3.01": "distress",
    "4.01": "distress",
}

#: Compensation mechanics: grants, option exercises, tax withholding, gifts.
_GRANT_CODES: Final[frozenset[str]] = frozenset({"A", "M", "F", "G", "C", "X"})


def categorize(source: str, event_type: str, payload: Mapping[str, Any]) -> str:
    if source == "system":
        return "system"
    if source == "halts":
        return "halt"
    if source == "edgar_13dg":
        return "activist_stake" if event_type == "13d" else "other"
    if source == "edgar_8k":
        items = [i for i in (payload.get("items") or []) if i in ITEM_BANDS]
        if not items:
            return "other"
        # The same "most serious item decides" rule the scorer uses, so the
        # category and the score always describe the same item.
        worst = max(items, key=lambda i: ITEM_BANDS[i])
        return _8K_ITEM_CATEGORY.get(worst, "other")
    if source == "edgar_form4":
        codes = {str(c).upper() for c in (payload.get("codes") or [])}
        if payload.get("is_open_market_purchase") or "P" in codes:
            return "insider_buy"
        if "S" in codes:
            # A sale alongside an option exercise is still, for the reader, a sale.
            return "insider_sell"
        if codes and codes <= _GRANT_CODES:
            return "grant_award"
        return "other"
    return "other"
