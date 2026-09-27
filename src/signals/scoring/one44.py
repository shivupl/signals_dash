"""Score a Form 144: an affiliate's notice that they intend to sell.

Where a Form 4 is a receipt, a 144 is advance warning -- filed on or before the
sale, naming the amount, the broker and the approximate date. That makes it the
earliest public signal of insider selling, which is also why it needs a quiet
calibration: most notices are a scheduled trickle out of a vested position, and a
feed that shouts about every one of them stops being read.

So the base is deliberately below both display thresholds. A notice becomes a flag
when it is large in dollars, large as a fraction of the company, or one of several
inside a month. A 10b5-1 plan *lowers* the score: the decision to sell was made
when the plan was adopted, so the filing says nothing about today.

Pure, like every scorer here. The cluster count comes from the database; this
module never reads it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ..models import Score
from .tables import (
    FORM144_BASE,
    FORM144_CLUSTER,
    FORM144_CLUSTER_MIN_SELLERS,
    FORM144_CONCENTRATED,
    FORM144_CONCENTRATED_PERCENT,
    FORM144_INSIDER,
    FORM144_LARGE_DOLLAR,
    FORM144_LARGE_DOLLAR_THRESHOLD,
    FORM144_PLAN_10B5_1,
)


@dataclass(frozen=True, slots=True)
class Form144Facts:
    """Everything the scorer needs, and nothing that requires a re-fetch."""

    value: float = 0.0
    #: The sale as a percentage of shares outstanding, when the filing says.
    percent_outstanding: float | None = None
    #: Officer, director or 10% holder -- and not a former one, nor a relative.
    is_insider: bool = False
    plan_10b5_1: bool = False
    #: Sales by this seller in the three months before the notice.
    prior_sales: int = 0

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Form144Facts:
        percent = payload.get("percent_outstanding")
        return cls(
            value=float(payload.get("value") or 0.0),
            percent_outstanding=float(percent) if percent is not None else None,
            is_insider=bool(payload.get("is_insider")),
            plan_10b5_1=bool(payload.get("plan_10b5_1")),
            prior_sales=int(payload.get("prior_sales") or 0),
        )

    @property
    def is_concentrated(self) -> bool:
        """A large fraction of the company, which is what moves a price. On a
        small float a modest dollar figure can be the bigger signal."""
        return (self.percent_outstanding or 0.0) > FORM144_CONCENTRATED_PERCENT


def score_144(facts: Form144Facts, *, cluster_sellers: int = 1) -> Score:
    """Score a notice. ``cluster_sellers`` is distinct sellers in the window."""
    parts: dict[str, int] = {"base": FORM144_BASE}

    if facts.is_insider:
        parts["insider"] = FORM144_INSIDER
    if facts.value > FORM144_LARGE_DOLLAR_THRESHOLD:
        parts["large_dollar"] = FORM144_LARGE_DOLLAR
    if facts.is_concentrated:
        parts["concentrated"] = FORM144_CONCENTRATED
    if cluster_sellers >= FORM144_CLUSTER_MIN_SELLERS:
        parts["cluster"] = FORM144_CLUSTER
    if facts.plan_10b5_1:
        parts["plan_10b5_1"] = FORM144_PLAN_10B5_1

    return Score.of(parts)


#: The official phrasings are accurate and unreadable in a one-line row.
_SHORT_RELATIONSHIP: Final[tuple[tuple[str, str], ...]] = (
    ("immediate family", "Family member"),
    ("former officer", "Former officer"),
    ("former director", "Former director"),
    ("10% stockholder", "10% holder"),
    ("ten percent", "10% holder"),
)


def short_relationship(relationship: str) -> str:
    lowered = relationship.lower()
    for needle, label in _SHORT_RELATIONSHIP:
        if needle in lowered:
            return label
    return relationship


def _percent(value: float) -> str:
    """A sale can be a real amount of money and a rounding error of the company.
    Rendering that as "0.00% of shares" reads as missing data rather than small."""
    return "<0.01% of shares" if value < 0.005 else f"{value:.2f}% of shares"


def describe_144(
    facts: Form144Facts, who: str, relationship: str, cluster_sellers: int = 1
) -> tuple[str, str]:
    """Headline and provenance line for the feed."""
    headline = f"Planned sale by {who}"
    if facts.value:
        headline += f" · ${facts.value:,.0f}"

    bits = ["form 144 · edgar"]
    if facts.percent_outstanding is not None:
        bits.append(_percent(facts.percent_outstanding))
    if relationship:
        bits.append(short_relationship(relationship))
    if facts.plan_10b5_1:
        # Named because it is why the score is low.
        bits.append("10b5-1 plan")
    if facts.prior_sales:
        bits.append(f"{facts.prior_sales} sales in the past 3 months")
    if cluster_sellers >= FORM144_CLUSTER_MIN_SELLERS:
        bits.append(f"{cluster_sellers} insiders selling in 30 days")
    return headline, " · ".join(bits)
