"""Capture real payloads into tests/fixtures/. The only script that touches the network.

Parsers are built and tested against these files, never against live SEC or
Nasdaq. Offline tests are faster, they work on a plane, and -- given SEC's 10
req/s ceiling and ~10-minute IP bans -- they cannot take the real system down.

Saved payloads do drift from reality, so every capture is recorded in
MANIFEST.json with its URL, date and sha256. Re-run this a few times a year:

    make fixtures

Requires --allow-network, because the point of the flag is that you cannot reach
the network here by accident.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
MANIFEST = FIXTURES / "MANIFEST.json"

EDGAR_INDEX = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type={t}&count=100&output=atom"
)

TARGETS: list[tuple[str, str, bool]] = [
    # (relative path, url, needs_sec_user_agent)
    ("edgar/company_tickers.json", "https://www.sec.gov/files/company_tickers.json", True),
    ("edgar/atom_form4_current.xml", EDGAR_INDEX.format(t=urllib.parse.quote("4")), True),
    ("edgar/atom_8k_current.xml", EDGAR_INDEX.format(t=urllib.parse.quote("8-K")), True),
    ("edgar/atom_13d_current.xml", EDGAR_INDEX.format(t=urllib.parse.quote("SC 13D")), True),
    ("edgar/atom_13g_current.xml", EDGAR_INDEX.format(t=urllib.parse.quote("SC 13G")), True),
    (
        "edgar/submissions_CIK0000320193.json",
        "https://data.sec.gov/submissions/CIK0000320193.json",
        True,
    ),
    (
        "halts/tradehalts_current.xml",
        "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts",
        False,
    ),
    ("edgar/atom_144_current.xml", EDGAR_INDEX.format(t=urllib.parse.quote("144")), True),
    # Form 144 submissions, chosen for the shapes that break a naive parser. The
    # namespace prefix is set by the filing agent and comes in three styles, so a
    # parser that hard-codes one silently reads nothing at all.
    (
        "edgar/form144_officer_ns2.txt",  # ns2: prefix, Officer, no plan, $56.9M
        "https://www.sec.gov/Archives/edgar/data/1943896/000195824426000624/"
        "0001958244-26-000624.txt",
        True,
    ),
    (
        "edgar/form144_plan_own_prefix.txt",  # own: prefix, 10b5-1 plan, prior sales
        "https://www.sec.gov/Archives/edgar/data/1713683/000196858226001015/"
        "0001968582-26-001015.txt",
        True,
    ),
    (
        "edgar/form144_no_prefix.txt",  # default namespace, no prefix at all, $24.8M
        "https://www.sec.gov/Archives/edgar/data/2132472/000195004726009828/"
        "0001950047-26-009828.txt",
        True,
    ),
    (
        "edgar/form144_family_trust.txt",  # relationship is a family member, 9 prior sales
        "https://www.sec.gov/Archives/edgar/data/2137517/000196922326001041/"
        "0001969223-26-001041.txt",
        True,
    ),
    (
        "edgar/form144_former_officer.txt",  # "Former Officer", and an EMPTY plan element
        "https://www.sec.gov/Archives/edgar/data/1443669/000195004726009826/"
        "0001950047-26-009826.txt",
        True,
    ),
    (
        "edgar/form144_ten_percent.txt",  # "10% Stockholder", empty plan, small float
        "https://www.sec.gov/Archives/edgar/data/1106145/000195004726009827/"
        "0001950047-26-009827.txt",
        True,
    ),
]


def fetch(url: str, user_agent: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="required: this script is the one place that leaves the machine",
    )
    parser.add_argument(
        "--only",
        default="",
        help="capture just the targets whose path contains this substring",
    )
    args = parser.parse_args()
    if not args.allow_network:
        print("refusing to run without --allow-network", file=sys.stderr)
        return 2

    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent or "@" not in user_agent:
        print(
            "SEC_USER_AGENT must be set to a real contact address before capturing\n"
            'SEC fixtures, e.g. SEC_USER_AGENT="Signals/0.1 (you@example.com)".\n'
            "SEC returns 403 to anonymous clients.",
            file=sys.stderr,
        )
        return 2

    manifest: dict[str, dict[str, str]] = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text())

    failures = 0
    targets = [t for t in TARGETS if args.only in t[0]]
    if not targets:
        print(f"no target path contains {args.only!r}", file=sys.stderr)
        return 2
    for rel, url, _needs_ua in targets:
        target = FIXTURES / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = fetch(url, user_agent)
        except Exception as exc:  # noqa: BLE001 -- a capture failure is informational
            print(f"FAIL {rel}: {exc}", file=sys.stderr)
            failures += 1
            continue
        target.write_bytes(body)
        manifest[rel] = {
            "url": url,
            "captured_at": datetime.now(tz=UTC).isoformat(),
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": str(len(body)),
        }
        print(f"  {rel}  {len(body):,} bytes")
        time.sleep(0.5)  # well inside SEC's ceiling; this is not a hot path

    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"\nmanifest: {MANIFEST}")
    if failures:
        print(f"{failures} target(s) failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
