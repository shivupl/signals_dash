"""The corpus report: what history says the threshold would have meant.

Pure formatting over rows the store supplies, so it is worth testing directly --
the flags/day figure is the number the threshold will be argued about with.
"""

from __future__ import annotations

from datetime import date

from signals.backfill import corpus_report


def row(source: str, score: int, events: int, *, days: int = 10, priced: int | None = None):
    return {
        "source": source,
        "score": score,
        "events": events,
        "earliest": date(2026, 1, 2),
        "latest": date(2026, 9, 25),
        "days": days,
        "priced": events if priced is None else priced,
    }


class TestCorpusReport:
    def test_an_empty_corpus_says_so(self) -> None:
        assert corpus_report([], 30) == "corpus is empty"

    def test_counts_flags_at_the_threshold(self) -> None:
        text = corpus_report([row("edgar_8k", 60, 4), row("edgar_8k", 20, 6)], 30)
        assert "at threshold 30: 4 flags" in text

    def test_flags_per_day_uses_market_days_not_calendar_days(self) -> None:
        """Weekends file nothing; dividing by calendar days would flatter it."""
        text = corpus_report([row("edgar_8k", 60, 20, days=10)], 30)
        assert "over 10 market days = 2.0 flags/day" in text

    def test_a_lower_threshold_flags_more(self) -> None:
        rows = [row("edgar_144", 20, 50), row("edgar_8k", 60, 5)]
        assert "at threshold 20: 55 flags" in corpus_report(rows, 20)
        assert "at threshold 30: 5 flags" in corpus_report(rows, 30)

    def test_reports_price_coverage(self) -> None:
        """A corpus without price_at cannot answer an outcome question, so the gap
        has to be visible rather than discovered later."""
        text = corpus_report([row("edgar_form4", 0, 100, priced=75)], 30)
        assert "with price_at   75 (75.0%)" in text

    def test_breaks_down_by_source(self) -> None:
        text = corpus_report([row("edgar_8k", 60, 3), row("edgar_form4", 0, 9)], 30)
        assert "edgar_8k" in text
        assert "edgar_form4" in text
        assert "flags     3" in text

    def test_bands_land_on_the_thresholds_that_matter(self) -> None:
        from signals.backfill import BANDS

        starts = [low for low, _high in BANDS]
        assert 30 in starts, "the watchlist's bar"
        assert 50 in starts, "the index view's bar"
        assert 85 in starts, "the rule-only floor"


class TestFormFilter:
    """Repair runs: re-walk one form without re-hydrating everything else."""

    @staticmethod
    def _filings():
        return [
            {"form": "4", "accession": "a"},
            {"form": "SCHEDULE 13G", "accession": "b"},
            {"form": "SCHEDULE 13G/A", "accession": "c"},
            {"form": "8-K", "accession": "d"},
        ]

    def test_no_filter_keeps_everything(self) -> None:
        from signals.backfill import only_forms

        assert len(only_forms(self._filings(), [])) == 4

    def test_filters_on_the_base_form_so_amendments_come_too(self) -> None:
        from signals.backfill import only_forms

        got = only_forms(self._filings(), ["SCHEDULE 13G"])
        assert [f["accession"] for f in got] == ["b", "c"]

    def test_matching_is_case_insensitive(self) -> None:
        from signals.backfill import only_forms

        assert len(only_forms(self._filings(), ["schedule 13g"])) == 2

    def test_several_forms_at_once(self) -> None:
        from signals.backfill import only_forms

        got = only_forms(self._filings(), ["8-K", "4"])
        assert sorted(f["accession"] for f in got) == ["a", "d"]
