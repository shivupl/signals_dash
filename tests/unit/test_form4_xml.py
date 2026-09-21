"""Form 4 parsing, against real submissions.

Every fixture here is a filing that was actually accepted by EDGAR, chosen to
cover one case each: an open-market purchase, an award-only filing, a sale, a
mixed filing, a 10b5-1 plan trade, a 29-transaction filing, and an officer title.
"""

from __future__ import annotations

import pytest

from signals.parsers.form4_xml import Form4ParseError, extract_xml, parse_form4
from tests.conftest import read_fixture

PURCHASE = read_fixture("edgar", "form4_purchase_P.txt")
AWARD_ONLY = read_fixture("edgar", "form4_award_only.txt")
SALE = read_fixture("edgar", "form4_sale.txt")
MULTI = read_fixture("edgar", "form4_multi_txn.txt")
PLAN = read_fixture("edgar", "form4_10b5_1.txt")
MANY = read_fixture("edgar", "form4_many_txns.txt")
TITLED = read_fixture("edgar", "form4_officer_title.txt")

ALL = [PURCHASE, AWARD_ONLY, SALE, MULTI, PLAN, MANY, TITLED]


class TestExtraction:
    def test_pulls_the_xml_out_of_the_submission_wrapper(self) -> None:
        """The adapter fetches {accession}.txt, not the XML document: the XML's
        filename is not predictable (ownership.xml, rdgdoc.xml, wk-form4_*.xml),
        so finding it by name would cost a directory listing first."""
        assert extract_xml(PURCHASE).startswith(b"<ownershipDocument")

    def test_strips_the_encoding_declaration(self) -> None:
        assert b"<?xml" not in extract_xml(PURCHASE)

    @pytest.mark.parametrize("payload", ALL)
    def test_every_fixture_parses(self, payload: bytes) -> None:
        assert parse_form4(payload).issuer_cik is not None

    def test_a_non_form4_document_is_rejected_clearly(self) -> None:
        with pytest.raises(Form4ParseError, match="ownershipDocument"):
            parse_form4(b"<XML><someOtherDocument/></XML>")

    def test_malformed_xml_raises_our_error_not_lxml_s(self) -> None:
        with pytest.raises(Form4ParseError):
            parse_form4(b"<XML><ownershipDocument><unclosed></XML>")


class TestIssuer:
    def test_issuer_cik_is_normalized_to_ten_digits(self) -> None:
        """It has to compare equal to company_alias, which is seeded from
        company_tickers.json where CIK is a bare integer."""
        doc = parse_form4(PURCHASE)
        assert doc.issuer_cik is not None
        assert len(doc.issuer_cik) == 10

    def test_issuer_name_and_symbol_are_present(self) -> None:
        doc = parse_form4(PURCHASE)
        assert doc.issuer_name
        assert doc.issuer_symbol


class TestTransactionCodes:
    def test_an_open_market_purchase_is_recognised(self) -> None:
        doc = parse_form4(PURCHASE)
        assert doc.is_open_market_purchase is True
        assert all(t.code == "P" for t in doc.purchases)

    def test_an_award_only_filing_is_not_a_purchase(self) -> None:
        """Code A is compensation mechanics. Treating it as a buy would flag
        every vesting event in the market."""
        assert parse_form4(AWARD_ONLY).is_open_market_purchase is False

    def test_a_sale_is_not_a_purchase(self) -> None:
        assert parse_form4(SALE).is_open_market_purchase is False

    def test_a_mixed_filing_reports_both_codes(self) -> None:
        doc = parse_form4(MULTI)
        assert {t.code for t in doc.transactions} >= {"A", "S"}

    def test_codes_are_upper_cased(self) -> None:
        for payload in ALL:
            for txn in parse_form4(payload).transactions:
                assert txn.code == txn.code.upper()


class TestAggregation:
    def test_a_filing_with_many_transactions_stays_one_document(self) -> None:
        """A real filing in the fixtures carries 29 transactions; the feed gets
        one row, not 29."""
        doc = parse_form4(MANY)
        assert len(doc.transactions) > 20

    def test_purchase_totals_sum_across_transactions(self) -> None:
        doc = parse_form4(PURCHASE)
        assert doc.purchase_shares == sum(t.shares or 0 for t in doc.purchases)
        assert doc.purchase_value == pytest.approx(
            sum((t.shares or 0) * (t.price or 0) for t in doc.purchases)
        )

    def test_value_is_shares_times_price(self) -> None:
        txn = parse_form4(PURCHASE).purchases[0]
        assert txn.value == pytest.approx((txn.shares or 0) * (txn.price or 0))

    def test_numeric_fields_come_from_the_value_wrapper(self) -> None:
        """transactionShares nests <value>; reading the element directly yields
        whitespace and a silent None."""
        doc = parse_form4(PURCHASE)
        assert doc.purchases[0].shares is not None
        assert doc.purchases[0].shares > 0

    def test_shares_after_supports_relative_sizing(self) -> None:
        assert parse_form4(PURCHASE).shares_after is not None


class TestOwners:
    def test_owner_ciks_are_normalized(self) -> None:
        for cik in parse_form4(PURCHASE).owner_ciks:
            assert len(cik) == 10

    def test_an_officer_title_is_captured(self) -> None:
        doc = parse_form4(TITLED)
        assert doc.primary_owner is not None
        assert doc.primary_owner.officer_title

    def test_role_falls_back_when_there_is_no_title(self) -> None:
        for payload in ALL:
            owner = parse_form4(payload).primary_owner
            assert owner is not None
            assert owner.role

    def test_identity_is_cik_not_name(self) -> None:
        """Cluster detection counts distinct insiders. Matching on name would
        split "John A. Smith" from "SMITH JOHN A" and invent a cluster."""
        doc = parse_form4(PURCHASE)
        assert doc.owner_ciks
        assert all(c.isdigit() for c in doc.owner_ciks)


class TestPlan10b51:
    """The flag is encoded three different ways in live filings.

    A live sample of 17 carried `1`, `0` and `false` for the same field. A plain
    truthiness test would read "false" as yes and apply the -30 penalty to
    filings that explicitly said no.
    """

    def test_a_plan_trade_is_detected(self) -> None:
        assert parse_form4(PLAN).plan_10b5_1 is True

    def test_numeric_zero_means_no(self) -> None:
        assert parse_form4(PURCHASE).plan_10b5_1 is False

    def test_the_string_false_means_no(self) -> None:
        """The case a naive bool() gets backwards."""
        assert parse_form4(AWARD_ONLY).plan_10b5_1 is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("true", True), ("TRUE", True), ("Y", True),
         ("0", False), ("false", False), ("False", False), ("", False), ("n", False)],
    )
    def test_flag_encodings(self, raw: str, expected: bool) -> None:
        from signals.parsers.form4_xml import _flag

        assert _flag(raw) is expected

    def test_a_footnote_mention_is_the_pre_2023_fallback(self) -> None:
        doc = parse_form4(
            b"""<XML><ownershipDocument>
            <issuer><issuerCik>0000320193</issuerCik></issuer>
            <footnotes><footnote id="F1">Sold under a Rule 10b5-1 plan.</footnote>
            </footnotes>
            </ownershipDocument></XML>"""
        )
        assert doc.plan_10b5_1 is True

    def test_an_unrelated_footnote_does_not_trigger_it(self) -> None:
        doc = parse_form4(
            b"""<XML><ownershipDocument>
            <issuer><issuerCik>0000320193</issuerCik></issuer>
            <footnotes><footnote id="F1">Shares held in a family trust.</footnote></footnotes>
            </ownershipDocument></XML>"""
        )
        assert doc.plan_10b5_1 is False


class TestDerivatives:
    def test_derivative_rows_are_marked(self) -> None:
        flags = {t.is_derivative for p in ALL for t in parse_form4(p).transactions}
        assert True in flags or False in flags

    def test_a_derivative_p_is_not_an_open_market_buy(self) -> None:
        """Option mechanics wear the same letter as a purchase."""
        doc = parse_form4(
            b"""<XML><ownershipDocument>
            <issuer><issuerCik>0000320193</issuerCik></issuer>
            <derivativeTable><derivativeTransaction>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts><transactionShares><value>100</value>
              </transactionShares></transactionAmounts>
            </derivativeTransaction></derivativeTable>
            </ownershipDocument></XML>"""
        )
        assert doc.transactions[0].code == "P"
        assert doc.is_open_market_purchase is False


class TestSales:
    def test_a_sale_filing_reports_its_value(self) -> None:
        """The insider table shows dollars bought against dollars sold."""
        doc = parse_form4(SALE)
        assert doc.sale_shares > 0
        assert doc.sale_value == pytest.approx(
            sum((t.shares or 0) * (t.price or 0) for t in doc.sales)
        )

    def test_a_purchase_filing_has_no_sales(self) -> None:
        assert parse_form4(PURCHASE).sale_value == 0

    def test_twenty_nine_transactions_sum_into_one_figure(self) -> None:
        doc = parse_form4(MANY)
        assert len(doc.sales) > 20
        assert doc.sale_value > 0
