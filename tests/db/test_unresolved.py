"""The unresolved queue, against a real Postgres.

It is a work list. Before it was keyed, re-polling appended a row per poll and it
became a growth log instead -- 16 distinct entities, 6,668 rows, 26 a minute.
"""

from __future__ import annotations

import pytest

from signals.store.pg import PgStore

pytestmark = pytest.mark.pg


@pytest.fixture
async def store(pg_dsn: str):
    s = await PgStore.connect(pg_dsn)
    yield s
    await s.close()


async def test_recording_the_same_filing_twice_stores_one_row(store: PgStore) -> None:
    for _ in range(5):
        await store.record_unresolved("edgar_8k", "acc-1", "SIMON PROPERTY GROUP L P", {})
    assert await store.count_unresolved() == 1


async def test_different_filings_each_get_a_row(store: PgStore) -> None:
    await store.record_unresolved("edgar_8k", "acc-1", "Simon Property Group L P", {})
    await store.record_unresolved("edgar_8k", "acc-2", "IPALCO ENTERPRISES, INC.", {})
    assert await store.count_unresolved() == 2


async def test_the_same_id_from_different_sources_is_not_a_collision(store: PgStore) -> None:
    await store.record_unresolved("edgar_8k", "acc-1", "A", {})
    await store.record_unresolved("edgar_form4", "acc-1", "A", {})
    assert await store.count_unresolved() == 2


async def test_the_first_sighting_is_the_one_kept(store: PgStore) -> None:
    """seen_at should say when a filing was first seen, not most recently."""
    await store.record_unresolved("edgar_8k", "acc-1", "First name", {})
    await store.record_unresolved("edgar_8k", "acc-1", "Later name", {})
    rows = await store._pool.fetch("select raw_name from unresolved")
    assert [r["raw_name"] for r in rows] == ["First name"]
