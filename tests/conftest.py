"""Shared test fixtures.

The single most important thing in this file is the network kill-switch. Parsers
are built against saved payloads in tests/fixtures/, never against live SEC or
Nasdaq: a test that reaches the network is slow, flaky at 3am, and -- with SEC's
10 req/s ceiling and ~10-minute IP bans -- capable of taking the real system down
with it. Only scripts/capture_fixtures.py is allowed out, and it runs by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_socket import disable_socket, enable_socket

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network() -> None:
    """Fail loudly on any network use. Autouse: opt-out must be deliberate.

    Unix sockets stay allowed because asyncio builds its event-loop self-pipe
    from a socketpair -- blocking that breaks every async test for reasons that
    have nothing to do with the network. TCP to the outside world is what matters
    here, and that stays shut.
    """
    disable_socket(allow_unix_socket=True)
    yield
    enable_socket()


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def read_fixture(*parts: str) -> bytes:
    """Fixtures are raw bytes exactly as received -- parsers take bytes.

    No pre-parsed intermediate: that would test the capture script, not the parser.
    """
    return (FIXTURES.joinpath(*parts)).read_bytes()


# ---------------------------------------------------------------------------
# Postgres fixtures, for tests marked @pytest.mark.pg
#
# A real Postgres, never SQLite: what these tests exercise is
# "on conflict do nothing returning id", jsonb_array_elements_text and
# percentile_cont -- the exact behaviour a substitute engine would fake.
# ---------------------------------------------------------------------------

TEST_DB = "signals_test"


def _admin_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL unset; start the compose stack to run -m pg tests")
    return dsn


def _test_dsn() -> str:
    base = _admin_dsn()
    head, _, _tail = base.rpartition("/")
    return f"{head}/{TEST_DB}"


@pytest.fixture(scope="session")
async def pg_dsn() -> str:
    """Create a scratch database with the schema applied, once per session."""
    import asyncpg

    from signals.migrations import migrate

    enable_socket()
    admin = await asyncpg.connect(_admin_dsn())
    try:
        await admin.execute(f'drop database if exists "{TEST_DB}" with (force)')
        await admin.execute(f'create database "{TEST_DB}"')
    finally:
        await admin.close()

    dsn = _test_dsn()
    await migrate(dsn)
    return dsn


@pytest.fixture(autouse=True)
async def clean_pg(request: pytest.FixtureRequest, pg_dsn: str | None = None):
    """Start every Postgres-backed test from an empty database.

    Rolling back a transaction is not enough here: components under test open
    their own pools, so their writes commit on a different connection and would
    otherwise leak into the next test. Truncating is cheap on a dedicated test
    database and isolates regardless of who holds the connection.
    """
    if request.node.get_closest_marker("pg") is None:
        yield
        return

    import asyncpg

    dsn = request.getfixturevalue("pg_dsn")
    enable_socket()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "truncate event, unresolved, price_daily, company_alias, company, setting, "
            "universe_member restart identity cascade"
        )
    finally:
        await conn.close()
    yield


@pytest.fixture
async def pg(pg_dsn: str):
    """A direct connection, for tests that want to inspect rows themselves."""
    import asyncpg

    enable_socket()
    conn = await asyncpg.connect(pg_dsn)
    try:
        yield conn
    finally:
        await conn.close()
