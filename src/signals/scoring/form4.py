"""Score a Form 4.

Only an open-market purchase scores at all. A, M and F are compensation
mechanics -- grants, option exercises, shares withheld for tax -- and flagging
them would mean flagging every vesting event in the market. A sale says far less
than a purchase: insiders sell for houses, divorces and diversification, and buy
for one reason.

The function is pure, and that is load-bearing. Cluster promotion rescores old
events by calling this again with a new ``cluster_insiders`` count, so a promoted
score is byte-identical to what a fresh ingest would have produced. The database
supplies the count; the scorer never reads it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..models import Score
from .tables import (
    FORM4_BASE,
    FORM4_CLUSTER,
    FORM4_CLUSTER_MIN_INSIDERS,
    FORM4_DIRECTOR,
    FORM4_LARGE_DOLLAR,
    FORM4_LARGE_DOLLAR_THRESHOLD,
    FORM4_LARGE_STAKE,
    FORM4_LARGE_STAKE_FRACTION,
    FORM4_PLAN_10B5_1,
    FORM4_SENIOR_OFFICER,
)

# "President" must not match "Vice President" or "SVP" -- a vice president is not
# the signal the senior-officer bonus is for. Live titles in the fixtures include
# "SVP, Chief Business Officer" and "VP, Chief Accounting Officer" alongside
# "President and CEO", so the distinction is not hypothetical.
_VICE_PRESIDENT = re.compile(r"\b(?:vice[-\s]*president|[sereavp]{0,3}vp)\b", re.I)
_SENIOR = re.compile(r"\b(?:chief\s+executive|ceo|chief\s+financial|cfo|president)\b", re.I)
#: A vice president who is also CFO still counts; the title just has to earn it
#: on something other than the word "president".
_C_LEVEL = re.compile(r"\b(?:chief\s+executive|ceo|chief\s+financial|cfo)\b", re.I)


@dataclass(frozen=True, slots=True)
class Form4Facts:
    """Everything the scorer needs, and nothing that requires a re-fetch."""

    is_open_market_purchase: bool = False
    purchase_shares: float = 0.0
    purchase_value: float = 0.0
    shares_after: float | None = None
    officer_title: str | None = None
    is_officer: bool = False
    is_director: bool = False
    plan_10b5_1: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Form4Facts:
        return cls(
            is_open_market_purchase=bool(payload.get("is_open_market_purchase")),
            purchase_shares=float(payload.get("purchase_shares") or 0.0),
            purchase_value=float(payload.get("purchase_value") or 0.0),
            shares_after=(
                float(payload["shares_after"])
                if payload.get("shares_after") is not None
                else None
            ),
            officer_title=payload.get("officer_title"),
            is_officer=bool(payload.get("is_officer")),
            is_director=bool(payload.get("is_director")),
            plan_10b5_1=bool(payload.get("plan_10b5_1")),
        )

    @property
    def is_senior_officer(self) -> bool:
        title = self.officer_title or ""
        if not title:
            return False
        if _VICE_PRESIDENT.search(title):
            # "SVP, Chief Business Officer" is not a CEO/CFO/President, even
            # though the string contains an officer-sounding word.
            return bool(_C_LEVEL.search(title))
        return bool(_SENIOR.search(title))

    @property
    def is_large_relative_stake(self) -> bool:
        """The buy moved the insider's own position meaningfully.

        Compared in shares rather than dollars: it is the same comparison once
        price cancels, and it still works when the filing omits a price.
        """
        if not self.shares_after or self.shares_after <= 0:
            return False
        return self.purchase_shares > FORM4_LARGE_STAKE_FRACTION * self.shares_after


def score_form4(facts: Form4Facts, *, cluster_insiders: int = 1) -> Score:
    """Score a filing. ``cluster_insiders`` is distinct buyers in the window."""
    if not facts.is_open_market_purchase:
        # Sales, grants, exercises and tax withholding all land here.
        return Score.zero()

    parts: dict[str, int] = {"base": FORM4_BASE}

    if facts.is_large_relative_stake:
        parts["large_stake"] = FORM4_LARGE_STAKE
    if facts.is_senior_officer:
        parts["senior_officer"] = FORM4_SENIOR_OFFICER
    elif facts.is_director:
        parts["director"] = FORM4_DIRECTOR
    if cluster_insiders >= FORM4_CLUSTER_MIN_INSIDERS:
        parts["cluster"] = FORM4_CLUSTER
    if facts.purchase_value > FORM4_LARGE_DOLLAR_THRESHOLD:
        parts["large_dollar"] = FORM4_LARGE_DOLLAR
    if facts.plan_10b5_1:
        # Pre-scheduled months earlier, so it says nothing about what they think
        # today -- which is the only thing an insider buy is evidence of.
        parts["plan_10b5_1"] = FORM4_PLAN_10B5_1

    return Score.of(parts)


def describe_form4(facts: Form4Facts, who: str, cluster_insiders: int = 1) -> tuple[str, str]:
    """Headline and provenance line for the feed."""
    if not facts.is_open_market_purchase:
        return f"Form 4 filed by {who}", "form 4 · edgar"

    if cluster_insiders >= FORM4_CLUSTER_MIN_INSIDERS:
        headline = f"{_ordinal(cluster_insiders)} insider buy in 30 days"
    else:
        headline = f"Open-market buy by {who}"

    bits = ["form 4 · edgar"]
    if facts.officer_title:
        bits.append(facts.officer_title)
    if facts.purchase_value:
        bits.append(f"${facts.purchase_value:,.0f}")
    if facts.plan_10b5_1:
        bits.append("10b5-1 plan")
    return headline, " · ".join(bits)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
