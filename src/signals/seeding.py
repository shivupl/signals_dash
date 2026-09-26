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
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import yaml

ROOT = Path(__file__).resolve().parents[2]
WATCHLIST = ROOT / "config" / "watchlist.yml"
#: Committed. The real watchlist is gitignored -- what you hold is nobody's
#: business -- and a fresh clone seeds from this until you write your own.
WATCHLIST_EXAMPLE = ROOT / "config" / "watchlist.example.yml"
TICKERS_FIXTURE = ROOT / "tests" / "fixtures" / "edgar" / "company_tickers.json"
#: The S&P 500 monitor. Committed, refreshed by hand with `make sp500`, and
#: optional -- a clone without it seeds the hand-picked watchlist alone.
SP500 = ROOT / "config" / "sp500.yml"
#: Past this the snapshot is old enough that membership has drifted: the index
#: changes roughly twenty names a year.
STALE_AFTER_DAYS = 90


def watchlist_path() -> Path:
    return WATCHLIST if WATCHLIST.exists() else WATCHLIST_EXAMPLE


def load_sp500(path: Path | None = None) -> tuple[list[dict[str, str]], date | None]:
    """Index members, and the date the snapshot was captured.

    A missing file is not an error: the S&P monitor is optional.
    """
    path = path or SP500
    if not path.exists():
        return [], None
    data = yaml.safe_load(path.read_text()) or {}
    members = [
        {"ticker": str(m["ticker"]).strip().upper(), "cik": str(m["cik"]).strip().zfill(10)}
        for m in data.get("members", [])
        if m.get("ticker") and m.get("cik")
    ]
    captured = data.get("captured")
    return members, captured if isinstance(captured, date) else None


def snapshot_age_days(captured: date | None, today: date) -> int | None:
    return None if captured is None else (today - captured).days


def load_watchlist(path: Path | None = None) -> list[str]:
    path = path or watchlist_path()
    if path == WATCHLIST_EXAMPLE:
        print(
            f"note: {WATCHLIST.name} not found; seeding from {WATCHLIST_EXAMPLE.name}. "
            "Copy it and edit to watch your own companies.",
            file=sys.stderr,
        )
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


async def seed(
    dsn: str,
    *,
    watchlist: list[str],
    filers: list[dict[str, Any]],
    sp500: list[dict[str, str]] | None = None,
) -> dict[str, int]:
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

            # Membership is authoritative from the files, so dropping a name
            # really stops watching it. Both universes are rebuilt, then
            # `watched` -- "ingest this company" -- is derived from them, which
            # keeps one source of truth instead of two that can disagree.
            #
            # Core matches through the alias table, so a watchlist naming a
            # secondary share class (GOOG rather than GOOGL) still marks the
            # company. The index matches on CIK: its 503 symbols are 500 filers,
            # and a ticker match would invent rows no filing can resolve to.
            core_ids = await conn.fetch(
                """
                select distinct company_id as id from company_alias
                where kind = 'ticker' and value = any($1::text[])
                """,
                watchlist,
            )
            sp_ids = await conn.fetch(
                "select id from company where cik = any($1::text[])",
                [m["cik"] for m in (sp500 or [])],
            )
            await conn.execute("delete from universe_member")
            await conn.executemany(
                "insert into universe_member (company_id, universe) values ($1, $2) "
                "on conflict do nothing",
                [(r["id"], "core") for r in core_ids] + [(r["id"], "sp500") for r in sp_ids],
            )
            changed = await conn.fetch(
                """
                update company set watched = exists (
                  select 1 from universe_member m where m.company_id = company.id
                )
                where watched <> exists (
                  select 1 from universe_member m where m.company_id = company.id
                )
                returning ticker
                """
            )
            watched_total = await conn.fetchval("select count(*) from company where watched")

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
        return {
            "companies": len(id_by_cik),
            "aliases": len(aliases),
            "watched": int(watched_total or 0),
            "changed": len(changed),
            "core": len(core_ids),
            "sp500": len(sp_ids),
        }
    finally:
        await conn.close()
