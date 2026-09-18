"""Form 4 scoring. Pure and table-driven."""

from __future__ import annotations

import pytest

from signals.parsers.form4_xml import parse_form4
from signals.scoring.form4 import Form4Facts, describe_form4, score_form4
from tests.conftest import read_fixture


def buy(**kw: object) -> Form4Facts:
    base: dict[str, object] = {"is_open_market_purchase": True, "purchase_shares": 1000.0}
    base.update(kw)
    return Form4Facts(**base)  # type: ignore[arg-type]


class TestOnlyPurchasesScore:
    def test_a_purchase_has_a_base_score(self) -> None:
        assert score_form4(buy()).total == 40

    def test_anything_that_is_not_a_purchase_scores_zero(self) -> None:
        """Grants, exercises, tax withholding and sales all arrive here. Flagging
        them would mean flagging every vesting event in the market."""
        assert score_form4(Form4Facts(is_open_market_purchase=False)).total == 0

    def test_a_non_purchase_scores_zero_even_with_every_other_signal(self) -> None:
        facts = Form4Facts(
            is_open_market_purchase=False,
            purchase_value=50_000_000.0,
            officer_title="Chief Executive Officer",
            is_director=True,
        )
        assert score_form4(facts, cluster_insiders=5).total == 0


class TestBonuses:
    def test_a_senior_officer_adds_fifteen(self) -> None:
        assert score_form4(buy(officer_title="Chief Executive Officer")).total == 55

    def test_a_director_adds_five(self) -> None:
        assert score_form4(buy(is_director=True)).total == 45

    def test_senior_officer_beats_director_rather_than_stacking(self) -> None:
        facts = buy(officer_title="Chief Financial Officer", is_director=True)
        assert score_form4(facts).total == 55

    def test_over_a_million_dollars_adds_ten(self) -> None:
        assert score_form4(buy(purchase_value=1_500_000.0)).total == 50

    def test_exactly_a_million_does_not(self) -> None:
        assert score_form4(buy(purchase_value=1_000_000.0)).total == 40

    def test_a_large_relative_stake_adds_twenty(self) -> None:
        """The buy moved the insider's own position meaningfully."""
        assert score_form4(buy(purchase_shares=1000.0, shares_after=10_000.0)).total == 60

    def test_a_small_relative_stake_does_not(self) -> None:
        assert score_form4(buy(purchase_shares=100.0, shares_after=1_000_000.0)).total == 40

    def test_relative_stake_needs_a_holdings_figure(self) -> None:
        assert score_form4(buy(shares_after=None)).total == 40

    def test_zero_holdings_does_not_divide_by_zero(self) -> None:
        assert score_form4(buy(shares_after=0.0)).total == 40


class TestSeniorOfficerMatching:
    @pytest.mark.parametrize(
        "title",
        ["Chief Executive Officer", "CEO", "Interim CEO", "President and CEO",
         "Chief Financial Officer", "CFO", "President", "President and CIO"],
    )
    def test_senior_titles(self, title: str) -> None:
        assert Form4Facts(officer_title=title).is_senior_officer is True

    @pytest.mark.parametrize(
        "title",
        ["Vice President", "SVP, Chief Business Officer", "VP, Chief Accounting Officer",
         "EVP, Operations", "Chief Technology Officer", "General Counsel", "Controller"],
    )
    def test_titles_that_must_not_count(self, title: str) -> None:
        """"Vice President" contains "President". A naive substring match promotes
        every VP in the company to the senior-officer bonus."""
        assert Form4Facts(officer_title=title).is_senior_officer is False

    def test_a_vice_president_who_is_also_cfo_still_counts(self) -> None:
        assert Form4Facts(officer_title="EVP, CFO").is_senior_officer is True

    def test_no_title_is_not_senior(self) -> None:
        assert Form4Facts(officer_title=None).is_senior_officer is False


class TestPlan10b51:
    def test_a_plan_trade_is_penalised(self) -> None:
        """Pre-scheduled months earlier, so it says nothing about what the insider
        thinks today -- which is the only thing a buy is evidence of."""
        assert score_form4(buy(plan_10b5_1=True)).total == 10

    def test_the_penalty_cannot_go_below_zero(self) -> None:
        assert score_form4(Form4Facts(is_open_market_purchase=True, plan_10b5_1=True)).total == 10


class TestCluster:
    def test_below_three_insiders_adds_nothing(self) -> None:
        assert score_form4(buy(), cluster_insiders=2).total == 40

    def test_three_insiders_adds_twenty_five(self) -> None:
        assert score_form4(buy(), cluster_insiders=3).total == 65

    def test_more_than_three_does_not_stack_further(self) -> None:
        assert score_form4(buy(), cluster_insiders=9).total == 65

    def test_the_case_that_changes_visibility(self) -> None:
        """A 10b5-1 buy sits at 10, below the threshold of 30, and is stored
        unflagged. The cluster bonus lifts it to 35 -- across the threshold. It is
        the only path where promotion changes what you see rather than the order
        you see it in."""
        facts = buy(plan_10b5_1=True)
        assert score_form4(facts, cluster_insiders=1).total == 10
        assert score_form4(facts, cluster_insiders=3).total == 35


class TestScoreParts:
    def test_parts_explain_the_total(self) -> None:
        score = score_form4(buy(officer_title="CFO", purchase_value=2_000_000.0))
        assert sum(score.parts.values()) == score.total
        assert set(score.parts) == {"base", "senior_officer", "large_dollar"}

    def test_parts_still_explain_the_total_when_clamped(self) -> None:
        """Every bonus at once sums past 100. The breakdown has to stay honest,
        or the tooltip answering "why is this a 100?" gives a wrong answer."""
        score = score_form4(
            buy(officer_title="CFO", purchase_value=2_000_000.0, shares_after=5000.0),
            cluster_insiders=3,
        )
        assert score.total == 100
        assert sum(score.parts.values()) == 100
        assert score.parts["clamped"] == -10

    def test_the_cluster_part_is_the_promotion_guard(self) -> None:
        """Promotion finds work by looking for rows with no cluster part, so its
        presence or absence has to be reliable."""
        assert "cluster" not in score_form4(buy(), cluster_insiders=1).parts
        assert "cluster" in score_form4(buy(), cluster_insiders=3).parts


class TestFromPayload:
    def test_reads_what_the_adapter_stored(self) -> None:
        facts = Form4Facts.from_payload(
            {
                "is_open_market_purchase": True,
                "purchase_shares": 500.0,
                "purchase_value": 25_000.0,
                "shares_after": 1000.0,
                "officer_title": "CEO",
                "is_director": True,
                "plan_10b5_1": False,
            }
        )
        assert facts.is_open_market_purchase is True
        assert facts.is_senior_officer is True
        assert facts.is_large_relative_stake is True

    def test_an_empty_payload_does_not_explode(self) -> None:
        assert score_form4(Form4Facts.from_payload({})).total == 0

    def test_a_real_purchase_filing_scores(self) -> None:
        """End to end from a filing EDGAR actually accepted."""
        from signals.adapters.edgar_form4 import EdgarForm4Adapter  # noqa: F401

        doc = parse_form4(read_fixture("edgar", "form4_purchase_P.txt"))
        facts = Form4Facts(
            is_open_market_purchase=doc.is_open_market_purchase,
            purchase_shares=doc.purchase_shares,
            purchase_value=doc.purchase_value,
            shares_after=doc.shares_after,
            officer_title=doc.primary_owner.officer_title if doc.primary_owner else None,
            is_director=bool(doc.primary_owner and doc.primary_owner.is_director),
            plan_10b5_1=doc.plan_10b5_1,
        )
        assert score_form4(facts).total >= 40

    def test_a_real_award_filing_scores_zero(self) -> None:
        doc = parse_form4(read_fixture("edgar", "form4_award_only.txt"))
        facts = Form4Facts(is_open_market_purchase=doc.is_open_market_purchase)
        assert score_form4(facts).total == 0


class TestDescribe:
    def test_a_single_buy_names_the_insider(self) -> None:
        headline, _ = describe_form4(buy(), "Jane Insider")
        assert headline == "Open-market buy by Jane Insider"

    def test_a_cluster_leads_with_the_count(self) -> None:
        headline, _ = describe_form4(buy(), "Jane Insider", cluster_insiders=3)
        assert headline == "3rd insider buy in 30 days"

    def test_the_detail_line_carries_provenance(self) -> None:
        _, detail = describe_form4(
            buy(officer_title="CFO", purchase_value=1_200_000.0, plan_10b5_1=True),
            "Jane",
        )
        assert "form 4" in detail and "CFO" in detail
        assert "$1,200,000" in detail
        assert "10b5-1" in detail

    @pytest.mark.parametrize(
        ("n", "expected"), [(3, "3rd"), (4, "4th"), (11, "11th"), (21, "21st"), (22, "22nd")]
    )
    def test_ordinals(self, n: int, expected: str) -> None:
        headline, _ = describe_form4(buy(), "x", cluster_insiders=n)
        assert headline.startswith(expected)
