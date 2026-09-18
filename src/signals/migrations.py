"""Forward-only migration runner.

Deliberately dumb: apply every db/*.sql not yet recorded, in filename order, each
in its own transaction. No down-migrations, no autogeneration. For a five-table
personal tool, Alembic is more machinery than the problem deserves.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

DB_DIR = Path(__file__).resolve().parents[2] / "db"

CREATE_VERSION_TABLE = """
create table if not exists schema_version (
  filename    text primary key,
  applied_at  timestamptz not null default now()
)
"""


def load_migrations(db_dir: Path = DB_DIR) -> list[tuple[str, str]]:
    """Read every migration before the event loop starts.

    File IO does not belong on the loop, and reading up front also means a
    mid-run disk error cannot leave the schema half-applied.
    """
    return [(p.name, p.read_text()) for p in sorted(db_dir.glob("[0-9]*.sql"))]


async def migrate(dsn: str, *, db_dir: Path = DB_DIR) -> list[str]:
    """Apply pending migrations. Returns the filenames applied."""
    pending = load_migrations(db_dir)
    conn = await asyncpg.connect(dsn)
    applied: list[str] = []
    try:
        await conn.execute(CREATE_VERSION_TABLE)
        done = {r["filename"] for r in await conn.fetch("select filename from schema_version")}
        for name, sql in pending:
            if name in done:
                continue
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("insert into schema_version (filename) values ($1)", name)
            applied.append(name)
    finally:
        await conn.close()
    return applied
