"""Seeding, against a real Postgres.

The interesting cases are all about a company having more than one identity:
several share classes under one CIK, and a watchlist that might name any of them.
"""

from __future__ import annotations

import pytest

from signals.seeding import load_filers, load_watchlist, seed

pytestmark = pytest.mark.pg

FILERS = [
    {"cik": "0000320193", "ticker": "AAPL", "name": "Apple Inc."},
    {"cik": "0001652044", "ticker": "GOOGL", "name": "Alphabet Inc."},
    {"cik": "0001652044", "ticker": "GOOG", "name": "Alphabet Inc."},
    {"cik": "0001067983", "ticker": "BRK-B", "name": "BERKSHIRE HATHAWAY INC"},
    {"cik": "0001067983", "ticker": "BRK-A", "name": "BERKSHIRE HATHAWAY INC"},
]


async def _seed(pg_dsn: str, watchlist: list[str]) -> dict[str, int]:
    return await seed(pg_dsn, watchlist=watchlist, filers=FILERS)


class TestCompanyLoading:
    async def test_one_row_per_cik_not_per_ticker(self, pg_dsn: str, pg: object) -> None:
        stats = await _seed(pg_dsn, ["AAPL"])
        assert stats["companies"] == 3, "GOOG/GOOGL and BRK-A/BRK-B share a CIK each"

    async def test_is_idempotent(self, pg_dsn: str, pg: object) -> None:
        first = await _seed(pg_dsn, ["AAPL"])
        second = await _seed(pg_dsn, ["AAPL"])
        assert first["companies"] == second["companies"]
        assert first["aliases"] == second["aliases"]


class TestAliases:
    async def test_every_share_class_gets_a_ticker_alias(self, pg_dsn: str, pg: object) -> None:
        """The bug this guards: building ticker aliases from company.ticker keeps
        only one class per CIK, and the other symbol silently stops resolving --
        about 2,400 of them across the real filer list."""
        import asyncpg

        await _seed(pg_dsn, ["AAPL"])
        conn = await asyncpg.connect(pg_dsn)
        try:
            rows = await conn.fetch(
                "select value from company_alias where kind='ticker' order by value"
            )
        finally:
            await conn.close()
        assert {r["value"] for r in rows} == {"AAPL", "BRK-A", "BRK-B", "GOOG", "GOOGL"}

    async def test_both_share_classes_point_at_one_company(self, pg_dsn: str, pg: object) -> None:
        import asyncpg

        await _seed(pg_dsn, ["AAPL"])
        conn = await asyncpg.connect(pg_dsn)
        try:
            rows = await conn.fetch(
                "select company_id from company_alias "
                "where kind='ticker' and value in ('GOOG','GOOGL')"
            )
        finally:
            await conn.close()
        assert len({r["company_id"] for r in rows}) == 1

    async def test_cik_alias_is_zero_padded(self, pg_dsn: str, pg: object) -> None:
        """The form the Form 4 XML uses, so resolution matches without conversion."""
        import asyncpg

        await _seed(pg_dsn, ["AAPL"])
        conn = await asyncpg.connect(pg_dsn)
        try:
            rows = await conn.fetch("select value from company_alias where kind='cik'")
        finally:
            await conn.close()
        assert all(len(r["value"]) == 10 for r in rows)


class TestWatchlist:
    async def test_marks_the_named_tickers(self, pg_dsn: str, pg: object) -> None:
        stats = await _seed(pg_dsn, ["AAPL", "GOOGL"])
        assert stats["watched"] == 2

    async def test_a_secondary_share_class_still_marks_the_company(
        self, pg_dsn: str, pg: object
    ) -> None:
        """Naming GOOG must watch Alphabet even though company.ticker is GOOGL."""
        stats = await _seed(pg_dsn, ["GOOG"])
        assert stats["watched"] == 1

    async def test_watched_is_authoritative_from_the_file(self, pg_dsn: str, pg: object) -> None:
        """Removing a ticker from watchlist.yml must actually stop watching it."""
        import asyncpg

        await _seed(pg_dsn, ["AAPL", "GOOGL"])
        await _seed(pg_dsn, ["AAPL"])
        conn = await asyncpg.connect(pg_dsn)
        try:
            rows = await conn.fetch("select ticker from company where watched")
        finally:
            await conn.close()
        assert {r["ticker"] for r in rows} == {"AAPL"}

    async def test_unknown_ticker_is_reported_not_fatal(self, pg_dsn: str, pg: object) -> None:
        """A delisted name (NKLA) should warn, not abort the seed."""
        stats = await _seed(pg_dsn, ["AAPL", "NOSUCHTICKER"])
        assert stats["watched"] == 1


class TestConfigLoading:
    def test_real_watchlist_has_no_duplicates(self) -> None:
        tickers = load_watchlist()
        assert len(tickers) == len(set(tickers))
        assert len(tickers) == 40

    def test_real_filer_file_parses(self) -> None:
        filers = load_filers()
        assert len(filers) > 9000
        assert all(len(f["cik"]) == 10 for f in filers)
        assert all(f["ticker"] == f["ticker"].upper() for f in filers)


class TestWatchlistFallback:
    def test_falls_back_to_the_example_when_no_private_list_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The real watchlist is gitignored, so a fresh clone has only the example."""
        from signals import seeding

        monkeypatch.setattr(seeding, "WATCHLIST", tmp_path / "missing.yml")
        assert seeding.watchlist_path() == seeding.WATCHLIST_EXAMPLE
        assert len(seeding.load_watchlist()) == 40

    def test_a_private_list_wins_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from signals import seeding

        mine = tmp_path / "watchlist.yml"
        mine.write_text("tickers:\n  - AAPL\n  - msft\n")
        monkeypatch.setattr(seeding, "WATCHLIST", mine)
        assert seeding.load_watchlist() == ["AAPL", "MSFT"]

    def test_the_example_ships_with_the_code(self) -> None:
        from signals import seeding

        assert seeding.WATCHLIST_EXAMPLE.exists()


class TestUniverses:
    """Membership is reconciled from the files, never accumulated."""

    async def test_watchlist_members_land_in_core(self, pg_dsn: str, pg) -> None:
        await seed(pg_dsn, watchlist=["AAPL"], filers=FILERS)
        rows = await pg.fetch(
            "select c.ticker from universe_member m join company c on c.id = m.company_id "
            "where m.universe = 'core'"
        )
        assert [r["ticker"] for r in rows] == ["AAPL"]

    async def test_sp500_members_are_marked_and_watched(self, pg_dsn: str, pg) -> None:
        await seed(
            pg_dsn,
            watchlist=["AAPL"],
            filers=FILERS,
            sp500=[{"ticker": "GOOGL", "cik": "0001652044"}],
        )
        watched = await pg.fetch("select ticker from company where watched order by ticker")
        assert {r["ticker"] for r in watched} == {"AAPL", "GOOGL"}
        universes = await pg.fetch(
            "select m.universe from universe_member m join company c on c.id = m.company_id "
            "where c.cik = '0001652044'"
        )
        assert [r["universe"] for r in universes] == ["sp500"]

    async def test_dual_share_classes_collapse_to_one_member(self, pg_dsn: str, pg) -> None:
        """503 index tickers are 500 filers: GOOGL and GOOG are one company, and a
        ticker match would invent a row no filing can ever resolve to."""
        await seed(
            pg_dsn,
            watchlist=[],
            filers=FILERS,
            sp500=[
                {"ticker": "GOOGL", "cik": "0001652044"},
                {"ticker": "GOOG", "cik": "0001652044"},
            ],
        )
        count = await pg.fetchval("select count(*) from universe_member where universe = 'sp500'")
        assert count == 1

    async def test_dropping_out_of_the_index_stops_ingest(self, pg_dsn: str, pg) -> None:
        await seed(
            pg_dsn, watchlist=[], filers=FILERS, sp500=[{"ticker": "GOOGL", "cik": "0001652044"}]
        )
        await seed(pg_dsn, watchlist=[], filers=FILERS, sp500=[])
        assert await pg.fetchval("select count(*) from universe_member") == 0
        assert await pg.fetchval("select count(*) from company where watched") == 0

    async def test_a_company_in_both_keeps_core_when_the_index_drops_it(
        self, pg_dsn: str, pg
    ) -> None:
        await seed(
            pg_dsn,
            watchlist=["AAPL"],
            filers=FILERS,
            sp500=[{"ticker": "AAPL", "cik": "0000320193"}],
        )
        await seed(pg_dsn, watchlist=["AAPL"], filers=FILERS, sp500=[])
        universes = await pg.fetch(
            "select m.universe from universe_member m join company c on c.id = m.company_id "
            "where c.ticker = 'AAPL'"
        )
        assert [r["universe"] for r in universes] == ["core"]
        assert await pg.fetchval("select watched from company where ticker = 'AAPL'") is True

    async def test_reports_both_universe_sizes(self, pg_dsn: str, pg) -> None:
        stats = await seed(
            pg_dsn,
            watchlist=["AAPL"],
            filers=FILERS,
            sp500=[{"ticker": "GOOGL", "cik": "0001652044"}],
        )
        assert stats["core"] == 1
        assert stats["sp500"] == 1
        assert stats["watched"] == 2


class TestSnapshotAge:
    """A stale snapshot is the quiet failure here: membership drifts and nothing
    says so."""

    def test_reports_age_in_days(self) -> None:
        from datetime import date

        from signals.seeding import snapshot_age_days

        assert snapshot_age_days(date(2026, 6, 27), date(2026, 9, 25)) == 90

    def test_is_none_when_the_file_carries_no_date(self) -> None:
        from datetime import date

        from signals.seeding import snapshot_age_days

        assert snapshot_age_days(None, date(2026, 9, 25)) is None


class TestLoadSp500:
    def test_missing_file_is_not_an_error(self, tmp_path) -> None:
        """A fresh clone must seed without the index file."""
        from signals.seeding import load_sp500

        members, captured = load_sp500(tmp_path / "absent.yml")
        assert members == []
        assert captured is None

    def test_reads_members_and_the_capture_date(self, tmp_path) -> None:
        from datetime import date

        from signals.seeding import load_sp500

        path = tmp_path / "sp500.yml"
        path.write_text(
            'captured: 2026-09-25\nmembers:\n  - {ticker: mmm, cik: "66740"}\n'
        )
        members, captured = load_sp500(path)
        assert members == [{"ticker": "MMM", "cik": "0000066740"}]
        assert captured == date(2026, 9, 25)

    def test_the_committed_snapshot_is_five_hundred_filers(self) -> None:
        """Guards the parse, the file, and the dual-class collapse in one line."""
        from signals.seeding import load_sp500

        members, captured = load_sp500()
        assert len(members) == 503
        assert len({m["cik"] for m in members}) == 500
        assert captured is not None
