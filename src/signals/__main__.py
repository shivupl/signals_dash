"""Entry point: python -m signals {worker|api|migrate|seed}."""

from __future__ import annotations

import asyncio
import os
import sys

COMMANDS = ("worker", "api", "migrate", "seed", "replay")


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL is unset", file=sys.stderr)
        raise SystemExit(2)
    return dsn


def cmd_migrate() -> int:
    from .migrations import migrate

    applied = asyncio.run(migrate(_dsn()))
    print(f"applied: {', '.join(applied)}" if applied else "nothing to apply; schema is current")
    return 0


def cmd_seed() -> int:
    from .seeding import load_filers, load_watchlist, seed

    stats = asyncio.run(seed(_dsn(), watchlist=load_watchlist(), filers=load_filers()))
    print(
        f"seeded {stats['companies']:,} companies, {stats['aliases']:,} aliases, "
        f"{stats['watched']} watched"
    )
    return 0


def cmd_replay(args: list[str]) -> int:
    from .replay import replay_all

    watched_only = "--watched-only" in args
    results = asyncio.run(replay_all(_dsn(), watched_only=watched_only))
    for label, stats in results.items():
        print(
            f"{label:>4}: {stats['groups']:>3} filings  "
            f"{stats['resolved']:>3} resolved  {stats['watched']:>3} watched  "
            f"{stats['inserted']:>3} new  {stats['duplicate']:>3} duplicate"
        )
    return 0


def cmd_worker(args: list[str]) -> int:
    import logging

    from .config import Settings
    from .pipeline.supervisor import run_worker

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    # httpx logs every request at INFO. At a two-second poll across four sources
    # that is ~100,000 lines a day saying "200 OK"; failures still surface through
    # the runner's own warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)

    settings = Settings.from_env()
    # --all stores filings from every filer rather than only the watchlist. It
    # exists for verifying ingest at real volume; it is not how you run this.
    watched_only = "--all" not in args
    if not watched_only:
        logging.warning("--all: storing every filer's filings, not just the watchlist")
    try:
        asyncio.run(run_worker(settings, watched_only=watched_only))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    return 0


def cmd_api() -> int:
    from .api.app import serve

    return serve()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in COMMANDS:
        print(f"usage: python -m signals {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    command = args[0]
    if command == "migrate":
        return cmd_migrate()
    if command == "seed":
        return cmd_seed()
    if command == "replay":
        return cmd_replay(args[1:])
    if command == "api":
        return cmd_api()
    if command == "worker":
        return cmd_worker(args[1:])
    print(f"signals: {command} is not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
