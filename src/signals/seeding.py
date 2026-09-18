"""Load every SEC filer into `company`, then mark the watchlist.

All ~10,000 filers are loaded, not just the 40 watched ones. Resolution needs the
full map: an event arrives keyed by CIK and the question "which company is this?"
has to be answerable before the question "do I care?" can be. Marking a ticker
watched later is then a one-line update rather than a re-import.

Aliases are written at seed time so every Phase 1 lookup is a single indexed hit:
  ('cik', 0000320193) ('ticker', AAPL) ('legal_name', apple inc.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import asyncpg
import yaml

ROOT = Path(__file__).resolve().parents[2]
WATCHLIST = ROOT / "config" / "watchlist.yml"
TICKERS_FIXTURE = ROOT / "tests" / "fixtures" / "edgar" / "company_tickers.json"


def load_watchlist(path: Path = WATCHLIST) -> list[str]:
    data = yaml.safe_load(path.read_text())
    tickers = [str(t).strip().upper() for t in data.get("tickers", []) if str(t).strip()]
    duplicates = {t for t in tickers if tickers.count(t) > 1}
    if duplicates:
        raise ValueError(f"duplicate tickers in {path.name}: {sorted(duplicates)}")
    return tickers


def load_filers(path: Path = TICKERS_FIXTURE) -> list[dict[str, Any]]:
    """company_tickers.json is a dict keyed by row number, not a list."""
    raw = json.loads(path.read_text())
    rows = raw.values() if isinstance(raw, dict) else raw
    return [
        {
            "cik": str(r["cik_str"]).zfill(10),
            "ticker": str(r["ticker"]).strip().upper(),
            "name": str(r["title"]).strip(),
        }
        for r in rows
        if r.get("cik_str") and r.get("ticker")
    ]


async def seed(dsn: str, *, watchlist: list[str], filers: list[dict[str, Any]]) -> dict[str, int]:
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            # A filer can list several tickers under one CIK; keep the first and
            # let the alias table carry the rest.
            await conn.executemany(
                """
                insert into company (cik, ticker, name) values ($1, $2, $3)
                on conflict (cik) do update set
                  ticker = coalesce(company.ticker, excluded.ticker),
                  name   = excluded.name
                """,
                [(f["cik"], f["ticker"], f["name"]) for f in filers],
            )

            rows = await conn.fetch("select id, cik, ticker, name from company")
            id_by_cik = {r["cik"]: r["id"] for r in rows if r["cik"]}

            aliases: list[tuple[int, str, str]] = []
            for row in rows:
                if row["cik"]:
                    aliases.append((row["id"], "cik", row["cik"]))
                if row["name"]:
                    aliases.append((row["id"], "legal_name", row["name"].strip().lower()))

            # Tickers come from the source file, not from company.ticker. One CIK
            # often lists several share classes -- GOOG and GOOGL, BRK-A and
            # BRK-B -- and only one of them can occupy company.ticker. Building
            # ticker aliases from the company row instead would leave ~2,400
            # symbols unresolvable, so a halt on GOOG would land in the
            # unresolved queue even though Alphabet is watched.
            for filer in filers:
                company_id = id_by_cik.get(filer["cik"])
                if company_id is not None:
                    aliases.append((company_id, "ticker", filer["ticker"]))
            await conn.executemany(
                """
                insert into company_alias (company_id, kind, value) values ($1, $2, $3)
                on conflict (kind, value) do nothing
                """,
                aliases,
            )

            # Watched is authoritative from the file: dropping a ticker from
            # watchlist.yml must actually stop watching it.
            await conn.execute("update company set watched = false where watched")
            # Match through the alias table so a watchlist naming a secondary
            # share class (GOOG rather than GOOGL) still marks the company.
            marked = await conn.fetch(
                """
                update company set watched = true
                where id in (
                  select company_id from company_alias
                  where kind = 'ticker' and value = any($1::text[])
                )
                returning ticker
                """,
                watchlist,
            )

        resolved = await conn.fetch(
            """
            select value from company_alias
            where kind = 'ticker' and value = any($1::text[])
            """,
            watchlist,
        )
        found = {r["value"] for r in resolved}
        missing = sorted(set(watchlist) - found)
        if missing:
            print(f"warning: no SEC filer matches {', '.join(missing)}", file=sys.stderr)
        return {"companies": len(id_by_cik), "aliases": len(aliases), "watched": len(marked)}
    finally:
        await conn.close()
