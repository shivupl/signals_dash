"""The scoring constants, in one place so tuning is a diff you can read.

One 0-100 scale across every source, so the feed can sort a trading halt against
an insider buy. The hard cap matters more than the scale: under 20 flags a day,
or the thing stops getting read.
"""

from __future__ import annotations

from typing import Final

# --- 8-K -------------------------------------------------------------------
#
# (floor, ceiling) per item. Where floor == ceiling the rule is the whole answer.
# Where they differ, a classifier may later place the score inside the band --
# but only for bands below the rule-only threshold.
ITEM_BANDS: Final[dict[str, tuple[int, int]]] = {
    "4.02": (95, 95),  # prior financials cannot be relied on
    "1.03": (95, 95),  # bankruptcy or receivership
    "3.01": (85, 85),  # delisting notice / listing rule failure
    "4.01": (70, 70),  # auditor change
    "2.02": (60, 60),  # results of operations
    "5.02": (45, 55),  # officer/director departure or appointment
    "1.01": (40, 50),  # material definitive agreement
    "7.01": (20, 30),  # Reg FD disclosure
    "8.01": (20, 30),  # other events
}

# At or above this, the rule alone decides and no model is consulted. These
# items are unambiguous, and a model that could talk one of them down is a
# liability rather than a refinement.
RULE_ONLY_FLOOR: Final[int] = 85

# An 8-K carrying only items nobody scored. 9.01 (exhibits) is the usual case and
# is pure filing mechanics.
DEFAULT_ITEM_BAND: Final[tuple[int, int]] = (15, 15)

ITEM_LABELS: Final[dict[str, str]] = {
    "4.02": "Prior financials should not be relied on",
    "1.03": "Bankruptcy or receivership",
    "3.01": "Delisting notice or listing rule failure",
    "4.01": "Auditor change",
    "2.02": "Results of operations",
    "5.02": "Officer or director change",
    "1.01": "Material definitive agreement",
    "7.01": "Reg FD disclosure",
    "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}

# --- 13D / 13G -------------------------------------------------------------
#
# The distinction is the form type itself: 13D is activist intent, 13G is
# passive. No parsing required.
SCHEDULE_13D: Final[int] = 85
SCHEDULE_13G: Final[int] = 30
# Amendments are frequent and mostly restate a position that is already known.
SCHEDULE_13D_AMENDED: Final[int] = 45
SCHEDULE_13G_AMENDED: Final[int] = 15

# --- Form 4 ----------------------------------------------------------------
#
# Only code P -- an open-market purchase -- scores at all. A, M and F are
# compensation mechanics and are pure noise; S is a sale, which says far less
# than a purchase does.
FORM4_BASE: Final[int] = 40
FORM4_LARGE_STAKE: Final[int] = 20      # buy exceeds 5% of holdings
FORM4_SENIOR_OFFICER: Final[int] = 15   # CEO / CFO / President
FORM4_DIRECTOR: Final[int] = 5
FORM4_CLUSTER: Final[int] = 25          # 3+ distinct insiders inside 30 days
FORM4_LARGE_DOLLAR: Final[int] = 10     # over $1M
FORM4_PLAN_10B5_1: Final[int] = -30     # pre-scheduled, so it signals nothing

FORM4_CLUSTER_MIN_INSIDERS: Final[int] = 3
FORM4_CLUSTER_WINDOW_DAYS: Final[int] = 30
FORM4_LARGE_DOLLAR_THRESHOLD: Final[float] = 1_000_000.0
FORM4_LARGE_STAKE_FRACTION: Final[float] = 0.05

# Sales. "Only purchases score" is right about the ordinary case and wrong about
# the tail: people sell to buy houses and to diversify, but nobody sells a quarter
# of a billion dollars of their own company for a kitchen.
#
# Measured over 3,051 sales in two years of watchlist history:
#     p50   $1.1M     noise
#     p90  $21.9M
#     p96    $50M     108 filings in two years -- about one a week
#     p99   $217M
#     max   $1.5B
# The brackets sit at the top few percent, where the innocent reading runs out.
FORM4_SALE_LARGE: Final[int] = 45
FORM4_SALE_HUGE: Final[int] = 60
FORM4_SALE_LARGE_THRESHOLD: Final[float] = 50_000_000.0     # ~p96
FORM4_SALE_HUGE_THRESHOLD: Final[float] = 250_000_000.0     # ~p99
#: A plan discounts a sale without erasing it. Unlike a buy -- where a schedule
#: removes the signal, because the signal *was* the decision to buy today -- a
#: nine-figure scheduled sale is still a decision somebody made.
FORM4_SALE_PLAN_10B5_1: Final[int] = -10

#: A selling *programme* is not news twelve times.
#:
#: The first cut of the sale brackets put eight Bezos filings at the top of the
#: feed, each a scheduled Amazon sale around a billion dollars. Across the corpus
#: the large-sale tail is dominated by repeats: Karp 12 filings, Bezos 12, Stevens
#: 11, Samueli 7 -- the top ten sellers account for roughly two thirds of every
#: large sale in two years. Flagging each execution recreates exactly the flood
#: the Form 144 recalibration removed, only with bigger numbers.
#:
#: So the information sits in the *first* large sale by that insider, and repeats
#: inside the window are damped below the bar unless they are enormous. Same shape
#: as the repeat-volatility-pause dampener in the halt table, for the same reason.
FORM4_SALE_REPEAT: Final[int] = -25
FORM4_SALE_REPEAT_WINDOW_DAYS: Final[int] = 90


# --- Form 144 ---------------------------------------------------------------
#
# A notice of intent to sell, filed before the sale. Quiet by design: roughly
# eighteen arrive a day across both monitors, and most are a scheduled trickle out
# of a vested position. Base 20 keeps those on the record and under both
# thresholds (30 for the watchlist, 50 for the index); what earns a flag is size,
# concentration on a small float, or a crowd heading for the exit.
FORM144_BASE: Final[int] = 20
# Was 10, which put a routine insider notice at exactly 30 -- the threshold -- and
# made 750 of the corpus's 2,373 flags one low-value form. A third of the feed was
# somebody saying they might sell some stock next month.
FORM144_INSIDER: Final[int] = 5            # officer, director or 10% holder
FORM144_LARGE_DOLLAR: Final[int] = 15     # over $10M
FORM144_CONCENTRATED: Final[int] = 10     # over 1% of shares outstanding
FORM144_CLUSTER: Final[int] = 25          # 3+ distinct sellers inside 30 days
#: Deepened from -10 so a *scheduled* large notice also falls clear of the bar
#: rather than landing on it. Sitting exactly on the threshold is the bug.
FORM144_PLAN_10B5_1: Final[int] = -15      # adopted months ago; calendar, not news

FORM144_LARGE_DOLLAR_THRESHOLD: Final[float] = 10_000_000.0
FORM144_CONCENTRATED_PERCENT: Final[float] = 1.0
FORM144_CLUSTER_MIN_SELLERS: Final[int] = 3
FORM144_CLUSTER_WINDOW_DAYS: Final[int] = 30

SENIOR_OFFICER_TITLES: Final[tuple[str, ...]] = (
    "chief executive",
    "ceo",
    "chief financial",
    "cfo",
    "president",
)

# --- Halts -----------------------------------------------------------------
#
# Codes as Nasdaq Trader publishes them. One correction to the original design:
# `M` is NOT a market-wide circuit breaker. In live data it is a per-stock
# five-minute volatility pause on NYSE / Arca / AMEX listings -- individual
# symbols, five-minute resumes. Only MWC1-3 are market-wide.
HALT_REASON_SCORES: Final[dict[str, int]] = {
    "T1": 90,    # news pending -- something lands in minutes
    "H10": 85,   # SEC trading suspension
    "H11": 85,   # regulatory concern
    "T12": 80,   # additional information requested by the exchange
    "H4": 75,    # non-compliance with listing requirements
    "H9": 70,    # filings not current
    "T6": 60,    # extraordinary market activity
    "T3": 45,    # news released, resumption times set
    "T2": 40,    # news released
    "T8": 35,    # ETF halt
    "T5": 35,    # single-stock trading pause
    "LUDP": 30,  # limit-up/limit-down volatility pause
    "LUDS": 30,  # LULD straddle
    "M": 30,     # volatility pause, non-Nasdaq listing
    "MWC1": 50,  # market-wide circuit breaker, level 1
    "MWC2": 50,
    "MWC3": 50,
}
HALT_DEFAULT_SCORE: Final[int] = 25
HALT_RESUME_SCORE: Final[int] = 10  # recorded on the timeline, never flagged

# A thin stock can trip a volatility pause dozens of times in a session -- one
# symbol in a captured feed was paused 31 times in a day. The first pause is
# information; the thirtieth is noise that would eat the whole daily flag budget.
VOLATILITY_REASONS: Final[frozenset[str]] = frozenset({"LUDP", "LUDS", "M", "T5"})
HALT_REPEAT_PAUSE_SCORE: Final[int] = 15

MARKET_WIDE_REASONS: Final[frozenset[str]] = frozenset({"MWC1", "MWC2", "MWC3"})

HALT_REASON_LABELS: Final[dict[str, str]] = {
    "T1": "Halted, news pending",
    "T2": "Halted, news released",
    "T3": "Halted, news released — resumption scheduled",
    "T5": "Single-stock trading pause",
    "T6": "Halted, extraordinary market activity",
    "T8": "ETF halted",
    "T12": "Halted, additional information requested",
    "H4": "Halted, listing non-compliance",
    "H9": "Halted, filings not current",
    "H10": "SEC trading suspension",
    "H11": "Halted, regulatory concern",
    "LUDP": "Volatility pause",
    "LUDS": "Volatility pause",
    "M": "Volatility pause",
    "MWC1": "Market-wide circuit breaker, level 1",
    "MWC2": "Market-wide circuit breaker, level 2",
    "MWC3": "Market-wide circuit breaker, level 3",
}

DEFAULT_FLAG_THRESHOLD: Final[int] = 30
