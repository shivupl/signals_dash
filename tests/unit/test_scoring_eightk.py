"""8-K scoring. Table-driven, because every row here is a tuning decision."""

from __future__ import annotations

import pytest

from signals.scoring.eightk import score_8k, summarize_8k
from signals.scoring.tables import RULE_ONLY_FLOOR


class TestItemBands:
    @pytest.mark.parametrize(
        ("items", "expected"),
        [
            (["4.02"], 95),  # restatement -- the critical case
            (["1.03"], 95),  # bankruptcy
            (["3.01"], 85),  # delisting notice
            (["4.01"], 70),  # auditor change
            (["2.02"], 60),  # earnings
            (["5.02"], 45),
            (["1.01"], 40),
            (["7.01"], 20),
            (["8.01"], 20),
        ],
    )
    def test_single_item(self, items: list[str], expected: int) -> None:
        assert score_8k(items).total == expected

    def test_most_serious_item_wins(self) -> None:
        """A filing announcing a restatement and an exhibit list is a restatement."""
        assert score_8k(["9.01", "4.02", "7.01"]).total == 95

    def test_order_does_not_matter(self) -> None:
        assert score_8k(["7.01", "4.02"]).total == score_8k(["4.02", "7.01"]).total

    def test_real_multi_item_filing(self) -> None:
        """Straight from the captured feed: 1.01 + 1.02 + 9.01."""
        assert score_8k(["1.01", "1.02", "9.01"]).total == 40


class TestUnknownItems:
    def test_no_items_scores_low_but_nonzero(self) -> None:
        """Still a filing worth recording; nothing is known about it yet."""
        score = score_8k([])
        assert 0 < score.total < 30

    def test_only_unscored_items_is_routine(self) -> None:
        """9.01 alone is filing mechanics -- exhibits, nothing more."""
        assert score_8k(["9.01"]).total < 30

    def test_unknown_item_does_not_crash(self) -> None:
        assert score_8k(["99.99"]).total >= 0


class TestRuleOnlyGuarantee:
    @pytest.mark.parametrize("items", [["4.02"], ["1.03"], ["3.01"]])
    def test_severe_filings_never_reach_a_model(self, items: list[str]) -> None:
        """The guarantee lives in the scorer, not at the call site, so a future
        caller cannot forget it and let a model talk a restatement down."""
        score = score_8k(items)
        assert score.total >= RULE_ONLY_FLOOR
        assert score.needs_model is False

    def test_a_banded_item_is_offered_to_a_model(self) -> None:
        assert score_8k(["5.02"]).needs_model is True

    def test_a_fixed_item_is_not(self) -> None:
        assert score_8k(["2.02"]).needs_model is False


class TestScoreParts:
    def test_parts_name_the_deciding_item(self) -> None:
        """What the UI shows when you click a flag and ask why it is a 95."""
        assert "8k_item_4_02" in score_8k(["4.02", "9.01"]).parts


class TestSummaries:
    def test_names_the_item_and_its_meaning(self) -> None:
        assert summarize_8k(["4.02"]) == (
            "8-K Item 4.02 — Prior financials should not be relied on"
        )

    def test_counts_the_others(self) -> None:
        assert summarize_8k(["4.02", "9.01"]).endswith("(+1 more)")

    def test_handles_an_empty_filing(self) -> None:
        assert summarize_8k([]) == "8-K filed"

    def test_stays_short_enough_for_one_feed_row(self) -> None:
        for items in (["4.02"], ["5.02", "9.01"], ["1.01", "1.02", "9.01"]):
            assert len(summarize_8k(items)) < 90
