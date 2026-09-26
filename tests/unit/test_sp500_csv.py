"""The constituents CSV parser.

The trap this guards: the file has commas inside quoted fields ("Saint Paul,
Minnesota"), so splitting on commas reads the wrong column -- during measurement
that produced 369 CIKs out of 500.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signals.parsers.sp500_csv import Constituent, parse_constituents

SAMPLE = (
    b"Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded\n"
    b'MMM,3M,Industrials,Industrial Conglomerates,"Saint Paul, Minnesota",1957-03-04,66740,1902\n'
    b'GOOGL,Alphabet Inc. (Class A),Information Technology,Interactive Media,'
    b'"Mountain View, California",2014-04-03,1652044,1998\n'
    b'GOOG,Alphabet Inc. (Class C),Information Technology,Interactive Media,'
    b'"Mountain View, California",2006-04-03,1652044,1998\n'
)


class TestParseConstituents:
    def test_reads_ticker_and_cik(self) -> None:
        assert parse_constituents(SAMPLE)[0] == Constituent(
            ticker="MMM", cik="0000066740", name="3M"
        )

    def test_pads_ciks_to_ten_digits(self) -> None:
        assert all(len(row.cik) == 10 for row in parse_constituents(SAMPLE))

    def test_keeps_both_share_classes_of_one_filer(self) -> None:
        """503 index symbols are 500 filers. Both symbols survive parsing;
        collapsing them is the database's job, keyed on CIK."""
        rows = parse_constituents(SAMPLE)
        assert [r.ticker for r in rows if r.cik == "0001652044"] == ["GOOGL", "GOOG"]

    def test_rejects_a_file_missing_the_cik_column(self) -> None:
        with pytest.raises(ValueError, match="CIK"):
            parse_constituents(b"Symbol,Security\nMMM,3M\n")

    def test_skips_rows_with_no_cik(self) -> None:
        data = SAMPLE + b'BOGUS,Nothing,X,Y,"Nowhere, NA",2020-01-01,,1999\n'
        assert [r.ticker for r in parse_constituents(data)] == ["MMM", "GOOGL", "GOOG"]

    def test_tolerates_a_byte_order_mark(self) -> None:
        assert parse_constituents(b"\xef\xbb\xbf" + SAMPLE)[0].ticker == "MMM"


class TestAgainstTheRealShape:
    def test_parses_the_captured_sample(self, fixtures: Path) -> None:
        rows = parse_constituents((fixtures / "sp500" / "constituents_sample.csv").read_bytes())
        assert len(rows) == 3
        assert all(len(r.cik) == 10 and r.ticker and r.name for r in rows)
