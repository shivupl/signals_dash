"""Parse the Nasdaq Trader trade-halt RSS feed.

The feed covers every US listing, not only Nasdaq's -- live captures include
NYSE, NYSE Arca and AMEX symbols -- which is why it replaced the exchange page
the original design pointed at (a client-rendered app with no data in its HTML).

Dates and times arrive as separate Eastern-time strings, and they stay separate
here. Assembling them into an instant needs the project's one timezone
definition, which lives in ``clock`` -- and parsers do not import ``clock``. So
this module returns the pieces and the adapter does the assembly.

The same item mutates in place: resumption fields are empty while the halt is
open and fill in when it lifts. ``HaltItem.state`` names which of the three
observable states an item is in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

from lxml import etree

NDAQ = "http://www.nasdaqtrader.com/"

HaltState = Literal["open", "quoting", "resumed"]


@dataclass(frozen=True, slots=True)
class HaltItem:
    symbol: str
    name: str
    market: str
    reason: str
    halt_date: date
    halt_time: time
    threshold_price: float | None
    resumption_date: date | None
    resumption_quote_time: time | None
    resumption_trade_time: time | None

    @property
    def natural_key(self) -> str:
        """Identifies the halt itself, stable while the item mutates.

        Built from the halt's own fields and never from the resumption, so the
        key does not change when the resumption fields fill in.
        """
        return (
            f"{self.symbol}|{self.halt_date.isoformat()}|"
            f"{self.halt_time.isoformat()}|{self.reason}"
        )

    @property
    def state(self) -> HaltState:
        if self.resumption_trade_time is not None:
            return "resumed"
        if self.resumption_quote_time is not None:
            # Quotes have resumed but trading has not: a third observable state,
            # and the reason the resume event is keyed on the halt, not on a
            # resumption timestamp that is about to change.
            return "quoting"
        return "open"


def parse_halts(payload: bytes) -> list[HaltItem]:
    """Parse feed bytes. Items missing a symbol or a halt time are skipped."""
    # The feed opens with a UTF-8 byte-order mark, which lxml rejects when it
    # precedes the XML declaration.
    root = etree.fromstring(payload.lstrip(b"\xef\xbb\xbf"))  # noqa: S320
    out: list[HaltItem] = []
    for node in root.iter("item"):
        symbol = _field(node, "IssueSymbol")
        halt_date = _date(_field(node, "HaltDate"))
        halt_time = _time(_field(node, "HaltTime"))
        if not symbol or halt_date is None or halt_time is None:
            continue
        out.append(
            HaltItem(
                symbol=symbol.upper(),
                name=_field(node, "IssueName"),
                market=_field(node, "Market"),
                reason=_field(node, "ReasonCode").upper(),
                halt_date=halt_date,
                halt_time=halt_time,
                threshold_price=_price(_field(node, "PauseThresholdPrice")),
                resumption_date=_date(_field(node, "ResumptionDate")),
                resumption_quote_time=_time(_field(node, "ResumptionQuoteTime")),
                resumption_trade_time=_time(_field(node, "ResumptionTradeTime")),
            )
        )
    return out


def _field(node: etree._Element, name: str) -> str:
    found = node.find(f"{{{NDAQ}}}{name}")
    return (found.text or "").strip() if found is not None else ""


def _date(raw: str) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date()  # noqa: DTZ007 -- a calendar date
    except ValueError:
        return None


def _time(raw: str) -> time | None:
    """The feed uses both HH:MM:SS and HH:MM:SS.mmm, sometimes in one response."""
    if not raw:
        return None
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()  # noqa: DTZ007 -- a wall-clock time
        except ValueError:
            continue
    return None


def _price(raw: str) -> float | None:
    try:
        return float(raw) if raw else None
    except ValueError:
        return None
