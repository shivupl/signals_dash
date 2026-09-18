"""Replay captured fixtures into the database.

The live ingest loop is not built yet, but the parsers and the scorers are, and
the fixtures are real filings. Replaying them exercises the whole downstream
path -- parse, group, resolve, score, dedupe insert -- against genuine data and
without touching the network.

It is also the fastest honest way to see the feed. Events land with their real
acceptance timestamps, so latency figures computed from a replay are meaningless
and deliberately so: `ingested_at` is now, `occurred_at` is whenever SEC accepted
the filing, which may be hours ago.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .adapters.edgar_index import normalize_8k, normalize_13dg
from .models import NormalizedEvent, ResolvedEvent, Score
from .parsers.edgar_atom import group_by_accession, parse_atom
from .scoring.eightk import score_8k
from .scoring.thirteen_dg import score_13dg
from .store.pg import PgStore

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "edgar"


def score_for(event: NormalizedEvent) -> Score:
    if event.source == "edgar_8k":
        return score_8k(event.payload.get("items", []))
    if event.source == "edgar_13dg":
        return score_13dg(event.payload.get("form", ""))
    return Score.zero()


async def replay_file(
    store: PgStore,
    payload: bytes,
    normalize: object,
    *,
    watched_only: bool,
) -> dict[str, int]:
    """Parse one captured feed and store everything it yields."""
    groups = group_by_accession(parse_atom(payload))
    stats = {"groups": len(groups), "resolved": 0, "watched": 0, "inserted": 0, "duplicate": 0}

    for group in groups:
        event: NormalizedEvent = normalize(group)  # type: ignore[operator]
        company = await store.find_company(event.company_key)
        if company is not None:
            stats["resolved"] += 1
            if company.watched:
                stats["watched"] += 1
        else:
            await store.record_unresolved(
                event.source,
                event.external_id,
                event.company_key.name or "?",
                dict(event.payload),
            )

        if watched_only and (company is None or not company.watched):
            continue

        resolved = ResolvedEvent(
            normalized=event,
            company_id=company.id if company else None,
            score=score_for(event),
        )
        if await store.insert_event(resolved) is not None:
            stats["inserted"] += 1
        else:
            stats["duplicate"] += 1
    return stats


async def replay_all(dsn: str, *, watched_only: bool = False) -> dict[str, dict[str, int]]:
    store = await PgStore.connect(dsn)
    try:
        results: dict[str, dict[str, int]] = {}
        targets = [
            ("8-K", FIXTURES / "atom_8k_current.xml", normalize_8k),
            ("13D", FIXTURES / "atom_13d_current.xml", normalize_13dg),
            ("13G", FIXTURES / "atom_13g_current.xml", normalize_13dg),
        ]
        for label, path, normalize in targets:
            if not path.exists():
                continue
            # Read before awaiting: file IO does not belong on the event loop.
            results[label] = await replay_file(
                store, path.read_bytes(), normalize, watched_only=watched_only
            )
        return results
    finally:
        await store.close()


def now_utc() -> datetime:
    return datetime.now(tz=UTC)
