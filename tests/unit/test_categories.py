"""One vocabulary across sources."""

from __future__ import annotations

import pytest

from signals.categories import CATEGORY_LABELS, categorize


class TestForm4:
    @pytest.mark.parametrize(
        ("codes", "purchase", "expected"),
        [
            (["P"], True, "insider_buy"),
            (["S"], False, "insider_sell"),
            (["M", "S"], False, "insider_sell"),  # exercise-and-sell reads as a sale
            (["F", "M", "S"], False, "insider_sell"),
            (["A"], False, "grant_award"),
            (["F"], False, "grant_award"),
            (["F", "G"], False, "grant_award"),
            (["J"], False, "other"),
            ([], False, "other"),
        ],
    )
    def test_codes(self, codes: list[str], purchase: bool, expected: str) -> None:
        payload = {"codes": codes, "is_open_market_purchase": purchase}
        assert categorize("edgar_form4", "form4_other", payload) == expected

    def test_a_buy_wins_over_a_sale_in_the_same_filing(self) -> None:
        payload = {"codes": ["P", "S"], "is_open_market_purchase": True}
        assert categorize("edgar_form4", "form4_buy", payload) == "insider_buy"


class TestEightK:
    @pytest.mark.parametrize(
        ("items", "expected"),
        [
            (["5.02"], "officer_change"),
            (["1.01", "9.01"], "material_agreement"),
            (["2.02"], "earnings"),
            (["4.02"], "distress"),
            (["1.03"], "distress"),
            (["3.01", "5.02"], "distress"),
            (["8.01", "9.01"], "other"),
            ([], "other"),
        ],
    )
    def test_items(self, items: list[str], expected: str) -> None:
        assert categorize("edgar_8k", "8k", {"items": items}) == expected

    def test_category_and_score_describe_the_same_item(self) -> None:
        """Both use "most serious item decides". If they diverged, a row could be
        scored as a restatement and filed under "earnings"."""
        from signals.scoring.eightk import score_8k

        items = ["2.02", "4.02", "9.01"]
        assert categorize("edgar_8k", "8k", {"items": items}) == "distress"
        assert "8k_item_4_02" in score_8k(items).parts


class TestOtherSources:
    def test_activist_versus_passive(self) -> None:
        assert categorize("edgar_13dg", "13d", {}) == "activist_stake"
        assert categorize("edgar_13dg", "13g", {}) == "other"

    def test_halts_and_resumes_are_both_halts(self) -> None:
        assert categorize("halts", "halt", {}) == "halt"
        assert categorize("halts", "halt_resume", {}) == "halt"

    def test_system(self) -> None:
        assert categorize("system", "source_stale", {}) == "system"

    def test_every_result_has_a_label(self) -> None:
        for args in [("edgar_8k", "8k", {}), ("x", "y", {}), ("halts", "halt", {})]:
            assert categorize(*args) in CATEGORY_LABELS
