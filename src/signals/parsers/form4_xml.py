"""Parse a Form 4 ownership document.

Input is the *full submission text* -- ``{accession}.txt`` -- rather than the XML
document alone. That is deliberate: the XML's filename is not predictable.
Sampling live filings turns up ``ownership.xml``, ``rdgdoc.xml`` and
``wk-form4_1789609812.xml``, so locating it by name would need a directory
listing first. The submission text is one request, 5-25KB, and carries the XML
inside an ``<XML>`` wrapper.

The transaction code is the entire signal:

    P  open-market purchase   -- the one that matters
    S  sale
    A  grant or award         }
    M  option exercise        } compensation mechanics, pure noise
    F  shares withheld for tax}
    G  gift

One filing can hold many transactions -- a real one in the fixtures carries 29 --
so they are aggregated per filing and a single event is emitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from lxml import etree

from ..models import normalize_cik

# The ownership XML sits inside the SGML submission wrapper.
_XML_BLOCK = re.compile(rb"<XML>\s*(.*?)\s*</XML>", re.S | re.I)
_XML_DECL = re.compile(rb"<\?xml[^>]*\?>")

#: Open-market purchase. Everything else is either a sale or compensation
#: mechanics, and neither says what a purchase says.
PURCHASE_CODE: Final[str] = "P"
NOISE_CODES: Final[frozenset[str]] = frozenset({"A", "M", "F", "G"})

_FOOTNOTE_10B5_1 = re.compile(r"10b5[-\s]?1", re.I)


class Form4ParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Form4Owner:
    cik: str | None
    name: str
    is_officer: bool
    is_director: bool
    is_ten_percent: bool
    officer_title: str | None

    @property
    def role(self) -> str:
        if self.officer_title:
            return self.officer_title
        if self.is_officer:
            return "Officer"
        if self.is_director:
            return "Director"
        if self.is_ten_percent:
            return "10% owner"
        return "Insider"


@dataclass(frozen=True, slots=True)
class Form4Transaction:
    code: str
    shares: float | None
    price: float | None
    acquired_disposed: str | None
    shares_after: float | None
    security_title: str | None
    is_derivative: bool

    @property
    def value(self) -> float | None:
        if self.shares is None or self.price is None:
            return None
        return self.shares * self.price


@dataclass(frozen=True, slots=True)
class Form4Doc:
    issuer_cik: str | None
    issuer_name: str | None
    issuer_symbol: str | None
    owners: tuple[Form4Owner, ...]
    transactions: tuple[Form4Transaction, ...]
    plan_10b5_1: bool
    period_of_report: str | None

    @property
    def purchases(self) -> tuple[Form4Transaction, ...]:
        """Non-derivative open-market buys. Derivative code-P rows are option
        mechanics wearing the same letter and are not what the signal means."""
        return tuple(
            t for t in self.transactions if t.code == PURCHASE_CODE and not t.is_derivative
        )

    @property
    def is_open_market_purchase(self) -> bool:
        return bool(self.purchases)

    @property
    def purchase_shares(self) -> float:
        return sum(t.shares or 0.0 for t in self.purchases)

    @property
    def purchase_value(self) -> float:
        return sum(t.value or 0.0 for t in self.purchases)

    @property
    def sales(self) -> tuple[Form4Transaction, ...]:
        """Non-derivative open-market sales (code S)."""
        return tuple(t for t in self.transactions if t.code == "S" and not t.is_derivative)

    @property
    def sale_shares(self) -> float:
        return sum(t.shares or 0.0 for t in self.sales)

    @property
    def sale_value(self) -> float:
        return sum(t.value or 0.0 for t in self.sales)

    @property
    def shares_after(self) -> float | None:
        """Holdings after the last reported purchase, for relative sizing."""
        values = [t.shares_after for t in self.purchases if t.shares_after is not None]
        return max(values) if values else None

    @property
    def owner_ciks(self) -> tuple[str, ...]:
        """Insider identity for cluster detection.

        By CIK, never by name: "John A. Smith" and "SMITH JOHN A" are one person,
        and matching on the string would split them and fabricate a cluster.
        """
        return tuple(o.cik for o in self.owners if o.cik)

    @property
    def primary_owner(self) -> Form4Owner | None:
        return self.owners[0] if self.owners else None


def extract_xml(submission: bytes) -> bytes:
    """Pull the ownership XML out of the SGML submission wrapper."""
    match = _XML_BLOCK.search(submission)
    body = match.group(1) if match else submission
    # lxml refuses an encoding declaration on a str, and the wrapper may repeat
    # it; stripping is simpler than guessing.
    return _XML_DECL.sub(b"", body).strip()


def parse_form4(submission: bytes) -> Form4Doc:
    payload = extract_xml(submission)
    try:
        root = etree.fromstring(payload)  # noqa: S320 -- SEC document
    except etree.XMLSyntaxError as exc:
        raise Form4ParseError(f"not parseable as XML: {exc}") from exc

    if root.tag != "ownershipDocument":
        found = etree.QName(root).localname if isinstance(root.tag, str) else root.tag
        raise Form4ParseError(f"expected ownershipDocument, found {found!r}")

    return Form4Doc(
        issuer_cik=normalize_cik(_text(root, "issuer/issuerCik")),
        issuer_name=_text(root, "issuer/issuerName"),
        issuer_symbol=(_text(root, "issuer/issuerTradingSymbol") or None),
        owners=tuple(_owners(root)),
        transactions=tuple(_transactions(root)),
        plan_10b5_1=_plan_10b5_1(root),
        period_of_report=_text(root, "periodOfReport") or None,
    )


def _owners(root: etree._Element) -> list[Form4Owner]:
    out: list[Form4Owner] = []
    for node in root.findall("reportingOwner"):
        title = _text(node, "reportingOwnerRelationship/officerTitle") or None
        out.append(
            Form4Owner(
                cik=normalize_cik(_text(node, "reportingOwnerId/rptOwnerCik")),
                name=_text(node, "reportingOwnerId/rptOwnerName"),
                is_officer=_flag(_text(node, "reportingOwnerRelationship/isOfficer")),
                is_director=_flag(_text(node, "reportingOwnerRelationship/isDirector")),
                is_ten_percent=_flag(
                    _text(node, "reportingOwnerRelationship/isTenPercentOwner")
                ),
                officer_title=title,
            )
        )
    return out


def _transactions(root: etree._Element) -> list[Form4Transaction]:
    out: list[Form4Transaction] = []
    for table, derivative in (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    ):
        for node in root.findall(table):
            code = _value(node, "transactionCoding/transactionCode")
            if not code:
                continue
            out.append(
                Form4Transaction(
                    code=code.strip().upper(),
                    shares=_number(node, "transactionAmounts/transactionShares"),
                    price=_number(node, "transactionAmounts/transactionPricePerShare"),
                    acquired_disposed=_value(
                        node, "transactionAmounts/transactionAcquiredDisposedCode"
                    ),
                    shares_after=_number(
                        node,
                        "postTransactionAmounts/sharesOwnedFollowingTransaction",
                    ),
                    security_title=_value(node, "securityTitle"),
                    is_derivative=derivative,
                )
            )
    return out


def _plan_10b5_1(root: etree._Element) -> bool:
    """Whether the trade was pre-scheduled, and so says nothing about intent.

    Two shapes have to be handled. Since 2023 there is an ``aff10b5One``
    element -- but filers encode it inconsistently: live samples carry ``1``,
    ``0`` and ``false`` for the same field, so a plain truthiness test would
    treat an explicit "no" as a yes and penalise the filing. Older documents note
    it only in a footnote.
    """
    element = root.find("aff10b5One")
    if element is not None:
        text = _itertext(element).strip()
        if text:
            return _flag(text)

    footnotes = root.find("footnotes")
    if footnotes is not None:
        return bool(_FOOTNOTE_10B5_1.search(_itertext(footnotes)))
    return False


def _flag(text: str | None) -> bool:
    """EDGAR booleans arrive as 1/0 and true/false, sometimes in one corpus."""
    return (text or "").strip().lower() in {"1", "true", "y", "yes"}


def _itertext(node: etree._Element) -> str:
    """lxml's itertext can yield bytes for CDATA, so the parts are coerced."""
    return "".join(part.decode() if isinstance(part, bytes) else part for part in node.itertext())


def _text(node: etree._Element, path: str) -> str:
    found = node.find(path)
    if found is None:
        return ""
    return _itertext(found).strip()


def _value(node: etree._Element, path: str) -> str | None:
    """Most leaves wrap their content in <value>; a few carry it directly."""
    found = node.find(path)
    if found is None:
        return None
    inner = found.find("value")
    target = inner if inner is not None else found
    text = _itertext(target).strip()
    return text or None


def _number(node: etree._Element, path: str) -> float | None:
    raw = _value(node, path)
    if raw is None:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None
