"""Parsing a Form 144: a notice that an insider intends to sell.

Everything here is a real filing. The trap that shaped this parser: the XML's
namespace prefix is chosen by the filing agent and comes in at least three styles
-- ``ns2:``, ``own:`` and no prefix at all -- so a parser that hard-codes one
reads nothing and reports it as an empty filing rather than an error.
"""

from __future__ import annotations

import pytest

from signals.parsers.form144_xml import Form144ParseError, parse_form144
from tests.conftest import read_fixture

OFFICER_NS2 = read_fixture("edgar", "form144_officer_ns2.txt")
PLAN_OWN = read_fixture("edgar", "form144_plan_own_prefix.txt")
NO_PREFIX = read_fixture("edgar", "form144_no_prefix.txt")
FAMILY_TRUST = read_fixture("edgar", "form144_family_trust.txt")
FORMER_OFFICER = read_fixture("edgar", "form144_former_officer.txt")
TEN_PERCENT = read_fixture("edgar", "form144_ten_percent.txt")


class TestNamespaceStyles:
    """One assertion per style. Each of these was a live filing on one day."""

    def test_ns2_prefix(self) -> None:
        doc = parse_form144(OFFICER_NS2)
        assert doc.issuer_cik == "0001943896"
        assert doc.issuer_name == "Rubrik, Inc."
        assert doc.seller_name == "Bipul Sinha"

    def test_own_prefix(self) -> None:
        doc = parse_form144(PLAN_OWN)
        assert doc.issuer_name == "Zscaler, Inc."
        assert doc.seller_name == "Kevin Rubin"

    def test_no_prefix_at_all(self) -> None:
        doc = parse_form144(NO_PREFIX)
        assert doc.issuer_name == "Cerebras Systems Inc."
        assert doc.seller_name == "SEAN LIE"


class TestAmounts:
    def test_value_units_and_float(self) -> None:
        doc = parse_form144(OFFICER_NS2)
        assert doc.units == 500_000
        assert doc.value == pytest.approx(56_900_000.0)
        assert doc.units_outstanding == 167_367_143

    def test_decimal_values_parse(self) -> None:
        """aggregateMarketValue carries cents in most filings."""
        assert parse_form144(PLAN_OWN).value == pytest.approx(332_048.08)

    def test_percent_of_shares_outstanding(self) -> None:
        assert parse_form144(OFFICER_NS2).percent_outstanding == pytest.approx(0.2988, abs=1e-4)

    def test_percent_is_none_without_a_denominator(self) -> None:
        from dataclasses import replace

        doc = replace(parse_form144(OFFICER_NS2), units_outstanding=None)
        assert doc.percent_outstanding is None

    def test_a_small_company_makes_a_modest_sale_look_big(self) -> None:
        """Credit Acceptance: $7.8M, but on a 10.4M share float."""
        doc = parse_form144(FAMILY_TRUST)
        assert doc.units_outstanding == 10_380_580
        assert doc.percent_outstanding == pytest.approx(0.1355, abs=1e-3)


class TestRelationship:
    def test_officer(self) -> None:
        doc = parse_form144(OFFICER_NS2)
        assert doc.relationship == "Officer"
        assert doc.is_insider is True

    def test_former_officer_is_not_a_current_insider(self) -> None:
        """A former officer's sale says much less, and "Former Officer" contains
        the word "Officer" -- the same trap as "Vice President" in Form 4."""
        doc = parse_form144(FORMER_OFFICER)
        assert doc.relationship == "Former Officer"
        assert doc.is_insider is False

    def test_a_family_member_is_not_an_insider(self) -> None:
        doc = parse_form144(FAMILY_TRUST)
        assert doc.relationship.startswith("Member of immediate family")
        assert doc.is_insider is False

    def test_ten_percent_holder_counts_as_an_insider(self) -> None:
        doc = parse_form144(TEN_PERCENT)
        assert doc.relationship == "10% Stockholder"
        assert doc.is_insider is True


class TestPlan:
    def test_a_dated_plan_is_a_plan(self) -> None:
        doc = parse_form144(PLAN_OWN)
        assert doc.plan_10b5_1 is True
        assert doc.plan_dates == ("03/24/2026",)

    def test_an_absent_plan_element_is_not_a_plan(self) -> None:
        doc = parse_form144(OFFICER_NS2)
        assert doc.plan_10b5_1 is False
        assert doc.plan_dates == ()

    def test_an_empty_plan_element_is_not_a_plan(self) -> None:
        """Present but empty. Testing only for the element would call this a plan
        and quietly take 10 points off every such notice."""
        doc = parse_form144(FORMER_OFFICER)
        assert doc.plan_10b5_1 is False


class TestPriorSales:
    def test_counts_sales_in_the_past_three_months(self) -> None:
        doc = parse_form144(FAMILY_TRUST)
        assert doc.prior_sales == 9
        assert doc.prior_sales_value > 0

    def test_nothing_to_report_means_zero(self) -> None:
        doc = parse_form144(OFFICER_NS2)
        assert doc.prior_sales == 0
        assert doc.prior_sales_value == 0.0


class TestOtherFields:
    def test_broker_exchange_and_date(self) -> None:
        doc = parse_form144(NO_PREFIX)
        assert doc.broker.startswith("Morgan Stanley")
        assert doc.exchange == "NASDAQ"
        assert doc.approx_sale_date == "05/20/2026" or doc.approx_sale_date == "09/25/2026"

    def test_security_class(self) -> None:
        assert parse_form144(OFFICER_NS2).security_class == "Class A Common"


class TestFailures:
    def test_not_xml_is_an_error(self) -> None:
        with pytest.raises(Form144ParseError):
            parse_form144(b"<html>maintenance window</html>")

    def test_a_form_4_is_rejected(self) -> None:
        """The adapters share a hydration path; handing the wrong document to the
        wrong parser must fail loudly rather than produce an empty notice."""
        with pytest.raises(Form144ParseError):
            parse_form144(read_fixture("edgar", "form4_purchase_P.txt"))
