"""Scoring a Form 144: intent to sell, not a completed sale.

The calibration is deliberately quiet. About eighteen notices arrive a day across
both monitors, and most are a scheduled trickle out of a vested position -- worth
recording, not worth interrupting anyone. What earns a flag is size, concentration
on a small float, or several insiders heading for the exit at once.
"""

from __future__ import annotations

import pytest

from signals.scoring.one44 import Form144Facts, describe_144, score_144


def facts(**kw: object) -> Form144Facts:
    base: dict[str, object] = {
        "value": 500_000.0,
        "percent_outstanding": 0.05,
        "is_insider": True,
        "plan_10b5_1": False,
        "prior_sales": 0,
    }
    return Form144Facts(**{**base, **kw})  # type: ignore[arg-type]


class TestBands:
    def test_a_routine_insider_notice_is_recorded_not_flagged(self) -> None:
        """Base 20 plus the insider bonus: below both thresholds on purpose.

        The bonus was 10 until the corpus showed that putting this case at exactly
        30 made it a third of every flag in two years.
        """
        assert score_144(facts()).total == 25

    def test_a_scheduled_sale_scores_lower_still(self) -> None:
        """A 10b5-1 plan was adopted months ago, so today's filing is calendar
        mechanics rather than a decision."""
        assert score_144(facts(plan_10b5_1=True)).total == 10

    def test_a_large_dollar_sale_flags(self) -> None:
        """The Rubrik CEO's real notice: $56.9M, 0.3% of shares, Officer."""
        score = score_144(facts(value=56_900_000.0, percent_outstanding=0.2988))
        assert score.total == 40
        assert score.parts == {"base": 20, "insider": 5, "large_dollar": 15}

    def test_concentration_counts_even_when_the_dollars_are_modest(self) -> None:
        """A small float makes a modest sale material. Credit Acceptance: $7.8M
        on a 10.4M share count is 0.14% -- under the bar; 1.5% would not be."""
        assert score_144(facts(value=2_000_000.0, percent_outstanding=1.5)).total == 35

    def test_a_non_insider_seller_is_barely_scored(self) -> None:
        """A family trust or an unaffiliated holder selling is not the signal."""
        assert score_144(facts(is_insider=False)).total == 20

    def test_a_cluster_of_sellers_flags_everywhere(self) -> None:
        score = score_144(facts(), cluster_sellers=3)
        assert score.total == 50
        assert score.parts["cluster"] == 25

    def test_two_sellers_is_not_a_cluster(self) -> None:
        assert "cluster" not in score_144(facts(), cluster_sellers=2).parts

    def test_the_worst_case_still_ranks_below_a_bankruptcy(self) -> None:
        """Every bonus at once tops out at 80, by design: an intention to sell
        should not outrank a restatement (95) or a delisting notice (85). Nothing
        here can be clamped, so the parts always sum to the total."""
        score = score_144(
            facts(value=500_000_000.0, percent_outstanding=40.0), cluster_sellers=9
        )
        assert score.total == 75
        assert sum(score.parts.values()) == 75
        assert "clamped" not in score.parts

    def test_never_needs_a_model(self) -> None:
        assert score_144(facts()).needs_model is False


class TestFactsFromPayload:
    def test_reads_what_the_adapter_stores(self) -> None:
        got = Form144Facts.from_payload(
            {
                "value": 1_234.5,
                "percent_outstanding": 0.9,
                "is_insider": True,
                "plan_10b5_1": True,
                "prior_sales": 4,
            }
        )
        assert got.value == pytest.approx(1_234.5)
        assert got.plan_10b5_1 is True
        assert got.prior_sales == 4

    def test_a_payload_missing_everything_does_not_explode(self) -> None:
        assert score_144(Form144Facts.from_payload({})).total == 20


class TestDescribe:
    def test_headline_names_the_seller_and_the_money(self) -> None:
        headline, detail = describe_144(
            facts(value=56_900_000.0, percent_outstanding=0.2988), "Sinha Bipul", "Officer"
        )
        assert headline == "Planned sale by Sinha Bipul · $56,900,000"
        assert "0.30% of shares" in detail
        assert "Officer" in detail

    def test_a_plan_is_named_because_it_is_why_the_score_is_low(self) -> None:
        _headline, detail = describe_144(facts(plan_10b5_1=True), "Rubin Kevin", "Officer")
        assert "10b5-1 plan" in detail

    def test_prior_sales_are_mentioned_when_there_are_any(self) -> None:
        _headline, detail = describe_144(facts(prior_sales=9), "Watson Trust", "Family member")
        assert "9 sales in the past 3 months" in detail

    def test_a_cluster_is_named(self) -> None:
        _headline, detail = describe_144(facts(), "Someone", "Officer", cluster_sellers=4)
        assert "4 insiders" in detail


class TestReadability:
    """Real filings, real strings: both of these looked wrong on screen."""

    def test_a_tiny_fraction_is_not_rendered_as_zero(self) -> None:
        """Zscaler's real notice is 0.0009% of the company. "0.00% of shares"
        reads as a missing number rather than a small one."""
        _headline, detail = describe_144(
            facts(percent_outstanding=0.00095), "Rubin Kevin", "Officer"
        )
        assert "<0.01% of shares" in detail
        assert "0.00%" not in detail

    def test_a_real_fraction_keeps_two_decimals(self) -> None:
        _headline, detail = describe_144(facts(percent_outstanding=0.2988), "X", "Officer")
        assert "0.30% of shares" in detail

    def test_the_official_family_phrasing_is_shortened(self) -> None:
        from signals.scoring.one44 import short_relationship

        assert (
            short_relationship("Member of immediate family of any of the foregoing")
            == "Family member"
        )

    def test_a_short_relationship_is_left_alone(self) -> None:
        from signals.scoring.one44 import short_relationship

        assert short_relationship("Officer") == "Officer"


class TestTheThresholdFlood:
    """750 of 2,373 flags in the corpus were Form 144 notices scoring exactly 30 --
    base plus the insider bonus, landing precisely on the bar. A third of the feed
    was one low-value form, which is the trap the plan named for 7.01/8.01 and this
    source walked straight into.

    The insider bonus drops to 5 so a routine notice is recorded and silent, and
    the plan discount deepens so a *scheduled* large sale does not sit on the
    boundary either. Size, concentration and crowds still flag.
    """

    def test_a_routine_insider_notice_no_longer_flags(self) -> None:
        assert score_144(facts()).total == 25

    def test_a_scheduled_routine_notice_is_quieter_still(self) -> None:
        assert score_144(facts(plan_10b5_1=True)).total == 10

    def test_a_large_notice_still_flags(self) -> None:
        assert score_144(facts(value=56_900_000.0)).total == 40

    def test_a_large_scheduled_notice_does_not_sit_on_the_threshold(self) -> None:
        """It used to land on exactly 30. A value equal to the bar is the whole
        problem, so it has to fall clear of it."""
        score = score_144(facts(value=56_900_000.0, plan_10b5_1=True))
        assert score.total == 25
        assert score.total < 30

    def test_concentration_still_flags(self) -> None:
        assert score_144(facts(percent_outstanding=1.5)).total == 35

    def test_a_cluster_still_flags_loudly(self) -> None:
        assert score_144(facts(), cluster_sellers=3).total == 50

    def test_no_routine_notice_reaches_the_threshold(self) -> None:
        """The invariant that matters, stated as the common case rather than as
        "no combination may equal 30".

        Chasing the latter makes every constant hostage to one threshold value,
        which is itself a runtime setting. What has to hold is that a notice with
        nothing remarkable about it -- no size, no concentration, no crowd --
        stays clear of the bar however the plan and insider flags fall. Those are
        58% of all notices.
        """
        from itertools import product

        for insider, plan in product((True, False), (True, False)):
            total = score_144(
                Form144Facts(
                    value=500_000.0, percent_outstanding=0.05, is_insider=insider, plan_10b5_1=plan
                )
            ).total
            assert total <= 25, (insider, plan, total)

    def test_a_notice_worth_flagging_clears_the_bar_rather_than_sitting_on_it(self) -> None:
        """Size or concentration has to land above 30, not on it."""
        assert score_144(facts(value=56_900_000.0)).total >= 35
        assert score_144(facts(percent_outstanding=1.5)).total >= 35
        assert score_144(facts(), cluster_sellers=3).total >= 35
