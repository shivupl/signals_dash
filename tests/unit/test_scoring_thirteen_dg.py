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


class TestTheRenamedForms:
    """EDGAR renamed these in 2025: SC 13G became SCHEDULE 13G.

    The old spelling was the only one recognised, so every activist stake filed
    from 2025 onward scored zero -- indistinguishable from a routine filing, and
    the single highest-value signal in the system. Found by backfilling two years
    and noticing 13D/G coverage stopped dead in December 2024.
    """

    def test_schedule_13d_is_still_an_activist_stake(self) -> None:
        assert score_13dg("SCHEDULE 13D").total == score_13dg("SC 13D").total

    def test_schedule_13g_is_still_passive(self) -> None:
        assert score_13dg("SCHEDULE 13G").total == score_13dg("SC 13G").total

    def test_amendments_survive_the_rename(self) -> None:
        assert score_13dg("SCHEDULE 13D/A").total == score_13dg("SC 13D/A").total
        assert score_13dg("SCHEDULE 13G/A").total == score_13dg("SC 13G/A").total

    def test_neither_spelling_scores_zero(self) -> None:
        for form in ("SC 13D", "SCHEDULE 13D", "SC 13G", "SCHEDULE 13G"):
            assert score_13dg(form).total > 0, form

    def test_the_parts_name_the_same_thing_either_way(self) -> None:
        assert set(score_13dg("SCHEDULE 13D").parts) == set(score_13dg("SC 13D").parts)


class TestAdaptersPollBothSpellings:
    def test_the_index_adapter_asks_for_both(self) -> None:
        from signals.adapters.edgar_index import build_edgar_adapters

        dg = next(a for a in build_edgar_adapters() if a.name == "edgar_13dg")
        assert set(dg.forms) == {  # type: ignore[attr-defined]
            "SC 13D",
            "SC 13G",
            "SCHEDULE 13D",
            "SCHEDULE 13G",
        }

    def test_the_sweep_recognises_both(self) -> None:
        from signals.adapters.edgar_backfill import _13DG

        assert {"SC 13D", "SCHEDULE 13D", "SC 13G", "SCHEDULE 13G"} <= _13DG
