"""Refresh config/sp500.yml from the published constituents list.

Run by hand a few times a year -- the index changes about twenty names annually:

    make sp500

Requires --allow-network for the reason capture_fixtures.py does: nothing in this
repo should reach the network by accident. The added/removed diff is printed so
index changes land as a reviewable commit rather than shifting at runtime.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from signals.parsers.sp500_csv import parse_constituents  # noqa: E402

SOURCE = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
TARGET = Path(__file__).resolve().parents[1] / "config" / "sp500.yml"
#: Below this the fetch returned something that is not the index, and overwriting
#: a good snapshot with it would quietly stop watching hundreds of companies.
MIN_MEMBERS = 400


def render(rows: list[tuple[str, str]], captured: str) -> str:
    lines = [
        "# S&P 500 membership, captured from a published constituents list.",
        "# Refresh with `make sp500`. The diff is the point: index changes should be",
        "# a commit you can see, not a silent runtime change.",
        "#",
        "# Matched on CIK, not ticker -- the 503 symbols are 500 filers, because of",
        "# dual share classes (GOOGL/GOOG, FOX/FOXA, NWS/NWSA).",
        f"captured: {captured}",
        f"source: {SOURCE}",
        "members:",
    ]
    lines += [f'  - {{ticker: {ticker}, cik: "{cik}"}}' for ticker, cik in rows]
    return "\n".join(lines) + "\n"


def existing_tickers() -> set[str]:
    if not TARGET.exists():
        return set()
    return {
        line.split("ticker:")[1].split(",")[0].strip()
        for line in TARGET.read_text().splitlines()
        if "ticker:" in line
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="required: this script is one of two places that leave the machine",
    )
    args = parser.parse_args()
    if not args.allow_network:
        print("refusing to run without --allow-network", file=sys.stderr)
        return 2

    request = urllib.request.Request(SOURCE, headers={"User-Agent": "Signals/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read()

    members = parse_constituents(body)
    if len(members) < MIN_MEMBERS:
        print(
            f"refusing to write: only {len(members)} members parsed, expected ~500",
            file=sys.stderr,
        )
        return 1

    before = existing_tickers()
    rows = sorted((m.ticker, m.cik) for m in members)
    TARGET.write_text(render(rows, datetime.now(tz=UTC).date().isoformat()))

    after = {ticker for ticker, _ in rows}
    print(f"{len(rows)} tickers, {len({cik for _, cik in rows})} filers -> {TARGET}")
    if before:
        print(f"added:   {', '.join(sorted(after - before)) or 'none'}")
        print(f"removed: {', '.join(sorted(before - after)) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
