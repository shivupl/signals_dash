"""Parse a Form 144: notice that an affiliate intends to sell restricted stock.

A Form 144 is filed *before* the sale, so it is advance warning where a Form 4 is
a receipt. It is also the richer document of the two for a sale: the dollar value,
the shares outstanding, the seller's stated relationship to the issuer, the
broker, any 10b5-1 plan adoption date and every sale that seller made in the past
three months are all declared fields -- no inference required.

Two things about the real corpus shaped this parser.

**The namespace prefix belongs to the filing agent, not the form.** Sampling one
day's filings turns up ``ns2:``, ``own:`` and no prefix at all, all valid, all for
the same schema. A parser that hard-codes one reads nothing and reports it as an
empty notice, which is worse than failing. Namespaces are therefore stripped
before anything is looked up.

**"Former Officer" contains "Officer".** Relationship is free-ish text -- seen:
``Officer``, ``Director``, ``10% Stockholder``, ``Affiliate``, ``Stockholder``,
``Former Officer``, ``Member of immediate family of any of the foregoing``. A
former officer selling, or a relative selling, says much less than the officer
selling, so the match excludes those explicitly. This is the same shape of bug as
matching "Vice President" when looking for a president in Form 4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from lxml import etree

from ..models import normalize_cik
from .form4_xml import extract_xml

#: Relationships that make a sale worth noticing: the person had access. No
#: trailing \b after the percent form: "%" is not a word character, so "10%
#: Stockholder" would not match one.
_INSIDER: Final[re.Pattern[str]] = re.compile(
    r"\b(?:officer|director)\b|\b10\s*%|\bten\s+percent\b", re.I
)
#: Checked first. A past insider, or somebody related to one, is not the signal.
_NOT_INSIDER: Final[re.Pattern[str]] = re.compile(
    r"\b(former|immediate family|spouse|trust for|estate of)\b", re.I
)


class Form144ParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Form144Doc:
    #: None only if the filing omits it, which no sampled filing did. The adapter
    #: falls back to the index's (Subject) CIK rather than dropping the notice.
    issuer_cik: str | None
    issuer_name: str
    seller_name: str
    relationship: str
    security_class: str
    units: float | None
    value: float | None
    units_outstanding: float | None
    approx_sale_date: str
    exchange: str
    broker: str
    plan_dates: tuple[str, ...]
    prior_sales: int
    prior_sales_value: float

    @property
    def plan_10b5_1(self) -> bool:
        """A dated plan only. The element is often present and empty."""
        return bool(self.plan_dates)

    @property
    def is_insider(self) -> bool:
        if _NOT_INSIDER.search(self.relationship):
            return False
        return bool(_INSIDER.search(self.relationship))

    @property
    def percent_outstanding(self) -> float | None:
        """The sale as a share of the company. On a small float, a modest dollar
        figure is a large fraction -- and the fraction is what moves a price."""
        if not self.units or not self.units_outstanding:
            return None
        return self.units / self.units_outstanding * 100


def _strip_namespaces(root: etree._Element) -> etree._Element:
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = etree.QName(element).localname
    return root


def _text(root: etree._Element, path: str) -> str:
    found = root.find(path)
    if found is None:
        return ""
    return "".join(
        part.decode() if isinstance(part, bytes) else part for part in found.itertext()
    ).strip()


def _number(root: etree._Element, path: str) -> float | None:
    raw = _text(root, path).replace(",", "").replace("$", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_form144(submission: bytes) -> Form144Doc:
    payload = extract_xml(submission)
    try:
        root = _strip_namespaces(etree.fromstring(payload))  # noqa: S320 -- SEC document
    except etree.XMLSyntaxError as exc:
        raise Form144ParseError(f"not parseable as XML: {exc}") from exc

    if root.tag != "edgarSubmission":
        raise Form144ParseError(f"expected edgarSubmission, found {root.tag!r}")
    submission_type = _text(root, ".//submissionType")
    if not submission_type.startswith("144"):
        raise Form144ParseError(f"not a Form 144: submissionType={submission_type!r}")

    plan_dates = tuple(
        part.strip()
        for part in re.split(r"[;,]", _text(root, ".//planAdoptionDates"))
        if part.strip()
    )
    prior = root.findall(".//securitiesSoldInPast3Months")
    prior_value = sum(_number(node, "grossProceeds") or 0.0 for node in prior)

    return Form144Doc(
        issuer_cik=normalize_cik(_text(root, ".//issuerCik")),
        issuer_name=_text(root, ".//issuerName"),
        seller_name=_text(root, ".//nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"),
        relationship=_text(root, ".//relationshipToIssuer"),
        security_class=_text(root, ".//securitiesToBeSoldInfo/securitiesClassTitle")
        or _text(root, ".//securitiesClassTitle"),
        units=_number(root, ".//noOfUnitsSold"),
        value=_number(root, ".//aggregateMarketValue"),
        units_outstanding=_number(root, ".//noOfUnitsOutstanding"),
        approx_sale_date=_text(root, ".//approxSaleDate"),
        exchange=_text(root, ".//securitiesExchangeName"),
        broker=_text(root, ".//brokerOrMarketmakerDetails/name") or _text(root, ".//broker/name"),
        plan_dates=plan_dates,
        prior_sales=len(prior),
        prior_sales_value=prior_value,
    )
