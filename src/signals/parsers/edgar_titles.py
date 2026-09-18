"""Parse an EDGAR atom entry title.

Titles look like::

    4 - DSTG VI Investments, L.P. (0002072430) (Reporting)
    4 - Chime Financial, Inc. (0001795586) (Issuer)
    8-K - AEye, Inc. (0001818644) (Filer)
    8-K/A - Some Corp (0001234567) (Filer)

The role is the whole reason this module exists. On a Form 4, ``(Reporting)`` is
the *insider* -- a person or a fund, never the company you are watching -- and
``(Issuer)`` is the company. Resolving a Form 4 from a Reporting title attributes
the filing to the wrong entity, so the role has to be extracted and respected.

The company name is free text and can itself contain parentheses, so the CIK and
role are matched from the *end* of the string rather than by splitting on "(".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ..models import normalize_cik


class Role(StrEnum):
    REPORTING = "Reporting"   # Form 4: the insider
    ISSUER = "Issuer"         # Form 4: the company
    FILER = "Filer"           # 8-K, 13D/G: the filing entity
    SUBJECT = "Subject"       # 13D/G: the company being reported on
    UNKNOWN = "Unknown"

    @classmethod
    def parse(cls, raw: str) -> Role:
        for role in cls:
            if role.value.lower() == raw.strip().lower():
                return role
        return cls.UNKNOWN


# Anchored at the end: name is greedy, so the last "(10 digits) (Role)" pair wins
# even when the company name contains its own parentheses.
_TITLE = re.compile(
    r"^\s*(?P<form>\S+(?:\s\S+)*?)\s+-\s+(?P<name>.+)\s+\((?P<cik>\d{6,10})\)\s+\((?P<role>[^()]+)\)\s*$"
)


@dataclass(frozen=True, slots=True)
class ParsedTitle:
    form: str
    name: str
    cik: str
    role: Role

    @property
    def is_amendment(self) -> bool:
        """13D/A and 8-K/A are frequent and mostly uninteresting; scored lower."""
        return self.form.endswith("/A")

    @property
    def base_form(self) -> str:
        return self.form[:-2] if self.is_amendment else self.form


def parse_title(title: str) -> ParsedTitle | None:
    """Return the parsed title, or None if it does not match the expected shape.

    None rather than an exception: a single unparseable title should not take
    down a poll of a hundred entries.
    """
    match = _TITLE.match(title)
    if not match:
        return None
    cik = normalize_cik(match.group("cik"))
    if cik is None:
        return None
    return ParsedTitle(
        form=match.group("form").strip(),
        name=match.group("name").strip(),
        cik=cik,
        role=Role.parse(match.group("role")),
    )
