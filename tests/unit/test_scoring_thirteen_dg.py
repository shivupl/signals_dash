"""13D/G scoring -- the form type is the entire signal."""

from __future__ import annotations

import pytest

from signals.scoring.thirteen_dg import score_13dg, summarize_13dg


class TestScores:
    @pytest.mark.parametrize(
        ("form", "expected"),
        [
            ("SC 13D", 85),    # activist intent
            ("13D", 85),
            ("SC 13G", 30),    # passive
            ("13G", 30),
            ("SC 13D/A", 45),  # amendments mostly restate what is known
            ("SC 13G/A", 15),
        ],
    )
    def test_score(self, form: str, expected: int) -> None:
        assert score_13dg(form).total == expected

    def test_activist_outscores_passive_by_a_lot(self) -> None:
        assert score_13dg("SC 13D").total > score_13dg("SC 13G").total + 40

    def test_amendment_is_quieter_than_the_original(self) -> None:
        assert score_13dg("SC 13D/A").total < score_13dg("SC 13D").total

    def test_case_and_spacing_are_tolerated(self) -> None:
        assert score_13dg("  sc 13d  ").total == 85

    def test_unrelated_form_scores_zero(self) -> None:
        assert score_13dg("10-K").total == 0


class TestSummaries:
    def test_distinguishes_activist_from_passive(self) -> None:
        assert "Activist" in summarize_13dg("SC 13D")
        assert "Passive" in summarize_13dg("SC 13G")

    def test_names_the_filer_when_known(self) -> None:
        assert summarize_13dg("SC 13D", "Elliott Management") == (
            "Activist stake disclosed by Elliott Management"
        )

    def test_marks_an_amendment(self) -> None:
        assert "amended" in summarize_13dg("SC 13D/A").lower()
