"""Domain model invariants."""

from __future__ import annotations

from datetime import datetime

import pytest

from signals.clock import EASTERN, UTC
from signals.models import CompanyKey, NormalizedEvent, Score, normalize_cik

NOW = datetime(2026, 9, 15, 20, 41, tzinfo=UTC)


class TestNormalizeCik:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (320193, "0000320193"),          # company_tickers.json: bare int
            ("320193", "0000320193"),
            ("0002073340", "0002073340"),    # Form 4 XML: already padded
            ("0000320193", "0000320193"),
            ("  320193 ", "0000320193"),
            (None, None),
            ("", None),
            ("not-a-cik", None),
        ],
    )
    def test_canonical_form(self, raw: object, expected: str | None) -> None:
        assert normalize_cik(raw) == expected  # type: ignore[arg-type]

    def test_the_three_source_formats_agree_after_normalizing(self) -> None:
        """The whole point: these are the same company and must compare equal."""
        from_tickers_json = normalize_cik(320193)
        from_form4_xml = normalize_cik("0000320193")
        assert from_tickers_json == from_form4_xml


class TestCompanyKey:
    def test_candidates_are_ordered_strongest_first(self) -> None:
        key = CompanyKey(cik="0000320193", ticker="aapl", name="  Apple Inc. ")
        assert key.candidates() == [
            ("cik", "0000320193"),
            ("ticker", "AAPL"),
            ("legal_name", "apple inc."),
        ]

    def test_empty_key_is_falsy(self) -> None:
        assert not CompanyKey()
        assert CompanyKey(ticker="AAPL")


class TestScore:
    def test_sums_parts(self) -> None:
        s = Score.of({"base": 40, "role_officer": 15})
        assert s.total == 55
        assert s.parts == {"base": 40, "role_officer": 15}

    def test_clamps_at_100(self) -> None:
        assert Score.of({"a": 80, "b": 50}).total == 100

    def test_clamps_at_zero_not_negative(self) -> None:
        """A 10b5-1 penalty on a small buy must floor at 0, never go negative."""
        assert Score.of({"base": 40, "plan_10b5_1": -30, "penalty": -50}).total == 0

    def test_ten_b5_1_buy_lands_at_ten(self) -> None:
        """The specific number that later gets promoted across the threshold."""
        assert Score.of({"base": 40, "plan_10b5_1": -30}).total == 10

    def test_ten_b5_1_buy_in_a_cluster_lands_at_thirty_five(self) -> None:
        """35 > threshold 30: this is the only path where promotion changes
        visibility rather than just ordering."""
        assert Score.of({"base": 40, "plan_10b5_1": -30, "cluster": 25}).total == 35

    def test_parts_are_copied_not_aliased(self) -> None:
        parts = {"base": 40}
        s = Score.of(parts)
        parts["base"] = 99
        assert s.parts == {"base": 40}


class TestNormalizedEventTimezone:
    def test_rejects_naive_occurred_at(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            NormalizedEvent(
                source="edgar_8k",
                external_id="0000320193-26-000001",
                event_type="8k",
                occurred_at=datetime(2026, 9, 15, 16, 5),  # noqa: DTZ001
                company_key=CompanyKey(cik="0000320193"),
                summary="Item 2.02",
            )

    def test_accepts_any_aware_timestamp(self) -> None:
        """EDGAR hands back -04:00 offsets; we do not force UTC at construction,
        only awareness. Conversion happens once, on the way to the database."""
        ev = NormalizedEvent(
            source="edgar_8k",
            external_id="x",
            event_type="8k",
            occurred_at=datetime(2026, 9, 15, 16, 5, tzinfo=EASTERN),
            company_key=CompanyKey(cik="0000320193"),
            summary="Item 2.02",
        )
        assert ev.occurred_at.utcoffset() is not None
