"""One vocabulary for "what kind of event is this", across every source.

Sources name things their own way -- an 8-K has item numbers, a Form 4 has
transaction codes, a halt has a reason code. The feed's event-type filter needs a
single small set that means the same thing everywhere, and this is the only place
that mapping is written down.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .scoring.tables import ITEM_BANDS

CATEGORY_LABELS: Final[dict[str, str]] = {
    "insider_buy": "Insider buy",
    "insider_sell": "Insider sell",
    # Intent, not a receipt: a Form 144 is filed before the sale. Kept apart from
    # insider_sell so "they have sold" and "they have said they will" can be read
    # -- and filtered -- separately.
    "sale_notice": "Planned sale (144)",
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


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """A source's name on screen, and what it is when you have forgotten.

    The hint exists because the dashboard shows a legend, and a legend written in
    the frontend drifts from the vocabulary written here. Order is the order the
    filter bar lists them in.
    """

    label: str
    hint: str


SOURCES: Final[dict[str, SourceInfo]] = {
    "edgar_8k": SourceInfo(
        "8-K",
        "Unscheduled material event — the catch-all filing. Item numbers say which kind: "
        "5.02 officer change, 2.02 earnings, 1.01 material agreement, 4.02 restatement.",
    ),
    "edgar_form4": SourceInfo(
        "Form 4",
        "An insider's completed trade, due within two business days. Transaction codes say "
        "which: P bought on the open market, S sold, A was granted it.",
    ),
    "edgar_13dg": SourceInfo(
        "13D/G",
        "Somebody crossed 5% of a company. 13D means they intend to influence it; 13G means "
        "they say they are passive. Amendments are frequent and usually routine.",
    ),
    "edgar_144": SourceInfo(
        "Form 144",
        "Notice of an intended sale of restricted stock, filed before the sale. A statement "
        "of intent, not a receipt — the trade may be smaller, later, or never.",
    ),
    "halts": SourceInfo(
        "Halts",
        "Trading paused by the exchange. Reason codes: LUDP volatility, T1 pending news, "
        "M market-wide. Real-time only — there is no history to backfill.",
    ),
    "system": SourceInfo(
        "System",
        "The pipeline talking about itself: a source gone quiet, the host suspended, a form "
        "type arriving that no adapter claims.",
    ),
}

#: Only the categories whose label alone would mislead or under-explain. A label
#: like "Insider buy" needs no gloss, and glossing it would bury the ones that do.
CATEGORY_HINTS: Final[dict[str, str]] = {
    "sale_notice": (
        "Filed before the sale, so it is intent rather than a receipt. Kept apart from an "
        "insider sell so the two can be read — and filtered — separately."
    ),
    "grant_award": (
        "Compensation mechanics: grants, option exercises, tax withholding, gifts. Recorded "
        "because the absence would be a hole, rarely news on its own."
    ),
    "distress": (
        "Restatement, bankruptcy, delisting notice, auditor resignation. The highest-scoring "
        "events in the system."
    ),
    "activist_stake": "A 13D: a 5%+ holder who states an intention to influence the company.",
    "other": (
        "Nothing in this vocabulary matched. A category that fills up is a sign the vocabulary "
        "has fallen behind what is being filed."
    ),
    "system": "Not about a company. Hidden from the feed by default; it has its own strip.",
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
    if source == "edgar_144":
        return "sale_notice"
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
