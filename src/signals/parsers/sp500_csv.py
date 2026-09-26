"""Parse the S&P 500 constituents CSV into (ticker, cik, name).

Pure: bytes in, dataclasses out. It lives in parsers/ because that is where the
layering contract keeps IO out -- fetching the file is the refresh script's job.

Real-world CSV with commas inside quoted fields, so it goes through
csv.DictReader. Splitting on commas reads the wrong column, and the wrong column
looks plausible: "1957-03-04" where a CIK should be.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Constituent:
    ticker: str
    cik: str
    name: str


def parse_constituents(data: bytes) -> list[Constituent]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    fields = reader.fieldnames or []
    for required in ("Symbol", "CIK"):
        if required not in fields:
            raise ValueError(f"constituents CSV has no {required} column; got {fields}")

    out: list[Constituent] = []
    for row in reader:
        raw_cik = (row.get("CIK") or "").strip()
        ticker = (row.get("Symbol") or "").strip().upper()
        if not raw_cik.isdigit() or not ticker:
            continue
        out.append(
            Constituent(
                ticker=ticker,
                cik=raw_cik.zfill(10),
                name=(row.get("Security") or ticker).strip(),
            )
        )
    return out
