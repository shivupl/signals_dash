"""Title parsing.

The role is the point. On a Form 4, (Reporting) is the insider and (Issuer) is
the company; getting them confused attributes a filing to a person who will never
be on anyone's watchlist.
"""

from __future__ import annotations

import pytest

from signals.parsers.edgar_titles import Role, parse_title


class TestWellFormedTitles:
    @pytest.mark.parametrize(
        ("title", "form", "name", "cik", "role"),
        [
            (
                "4 - DSTG VI Investments, L.P. (0002072430) (Reporting)",
                "4", "DSTG VI Investments, L.P.", "0002072430", Role.REPORTING,
            ),
            (
                "4 - Chime Financial, Inc. (0001795586) (Issuer)",
                "4", "Chime Financial, Inc.", "0001795586", Role.ISSUER,
            ),
            (
                "8-K - AEye, Inc. (0001818644) (Filer)",
                "8-K", "AEye, Inc.", "0001818644", Role.FILER,
            ),
            (
                "8-K/A - Some Corp (0001234567) (Filer)",
                "8-K/A", "Some Corp", "0001234567", Role.FILER,
            ),
            (
                "SC 13D - Target Co (0000320193) (Subject)",
                "SC 13D", "Target Co", "0000320193", Role.SUBJECT,
            ),
        ],
    )
    def test_parses(self, title: str, form: str, name: str, cik: str, role: Role) -> None:
        parsed = parse_title(title)
        assert parsed is not None
        assert (parsed.form, parsed.name, parsed.cik, parsed.role) == (form, name, cik, role)

    def test_company_name_containing_parentheses(self) -> None:
        """Names really do contain brackets, so the CIK is matched from the end."""
        parsed = parse_title("4 - Apple Inc. (California corp.) (0000320193) (Issuer)")
        assert parsed is not None
        assert parsed.name == "Apple Inc. (California corp.)"
        assert parsed.cik == "0000320193"
        assert parsed.role is Role.ISSUER

    def test_cik_is_normalized_to_ten_digits(self) -> None:
        """Short CIKs in a title must still match company_tickers.json entries."""
        parsed = parse_title("4 - Small Co (320193) (Issuer)")
        assert parsed is not None
        assert parsed.cik == "0000320193"


class TestAmendments:
    def test_detects_amendment(self) -> None:
        parsed = parse_title("SC 13D/A - Target Co (0000320193) (Filer)")
        assert parsed is not None
        assert parsed.is_amendment is True
        assert parsed.base_form == "SC 13D"

    def test_plain_form_is_not_an_amendment(self) -> None:
        parsed = parse_title("SC 13D - Target Co (0000320193) (Filer)")
        assert parsed is not None
        assert parsed.is_amendment is False
        assert parsed.base_form == "SC 13D"


class TestMalformed:
    @pytest.mark.parametrize(
        "title",
        [
            "",
            "no dash here",
            "4 - Missing Cik And Role",
            "4 - Name (notacik) (Issuer)",
            "garbage",
        ],
    )
    def test_returns_none_rather_than_raising(self, title: str) -> None:
        """One bad title must not abort a poll of a hundred entries."""
        assert parse_title(title) is None

    def test_unknown_role_is_captured_not_dropped(self) -> None:
        parsed = parse_title("4 - Name (0000320193) (Something New)")
        assert parsed is not None
        assert parsed.role is Role.UNKNOWN
