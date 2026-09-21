"""The company endpoint: everything about one name in one request."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import httpx
import pytest
from pytest_socket import enable_socket

from signals.api import deps
from signals.api.app import create_app
from signals.api.routes_company import insiders_from, name_stem
from signals.config import Settings
from signals.store.pg import PgStore

NOW = datetime.now(tz=UTC)


def form4(
    cik: str,
    name: str,
    *,
    days: int,
    bought: float = 0,
    sold: float = 0,
    codes: list[str] | None = None,
    cluster: bool = False,
    title: str | None = None,
):
    parts = {"base": 40, **({"cluster": 25} if cluster else {})}
    return {
        "occurred_at": NOW - timedelta(days=days),
        "url": "https://www.sec.gov/x",
        "score": 0,
        "payload": {
            "insider_ciks": [cik],
            "insider_names": [name],
            "officer_title": title,
            "codes": codes or ["S"],
            "purchase_value": bought,
            "sale_value": sold,
            "score_parts": parts,
        },
    }


class TestInsiderTable:
    def test_one_row_per_insider_keyed_on_cik(self) -> None:
        """The same person files under differently formatted names. Grouping by
        name would split their totals in half."""
        rows = [
            form4("0000000111", "SMITH JOHN A", days=1, sold=100_000),
            form4("0000000111", "John A. Smith", days=5, sold=50_000),
        ]
        people = insiders_from(rows, NOW)
        assert len(people) == 1
        assert people[0]["sold_90d"] == 150_000
        assert people[0]["filings"] == 2

    def test_only_the_last_ninety_days_are_summed(self) -> None:
        rows = [
            form4("0000000111", "A", days=10, bought=1000),
            form4("0000000111", "A", days=120, bought=9999),
        ]
        assert insiders_from(rows, NOW)[0]["bought_90d"] == 1000

    def test_the_latest_filing_supplies_role_and_codes(self) -> None:
        rows = [
            form4("0000000111", "A", days=1, codes=["P"], title="CFO"),
            form4("0000000111", "A", days=30, codes=["A"], title="VP"),
        ]
        person = insiders_from(rows, NOW)[0]
        assert (person["role"], person["last_codes"]) == ("CFO", ["P"])

    def test_buyers_sort_to_the_top(self) -> None:
        rows = [
            form4("0000000222", "Seller", days=1, sold=5_000_000),
            form4("0000000111", "Buyer", days=2, bought=10_000),
        ]
        assert [p["name"] for p in insiders_from(rows, NOW)] == ["Buyer", "Seller"]

    def test_cluster_membership_is_visible(self) -> None:
        """The one place cluster promotion can actually be seen."""
        rows = [form4("0000000111", "A", days=1, bought=1, cluster=True)]
        assert insiders_from(rows, NOW)[0]["in_cluster"] is True


class TestNameStem:
    @pytest.mark.parametrize(
        ("name", "stem"),
        [
            ("Simon Property Group, Inc.", "simon property"),
            ("Apple Inc.", "apple"),
            ("BERKSHIRE HATHAWAY INC", "berkshire hathaway"),
            ("FuelCell Energy, Inc.", "fuelcell energy"),
        ],
    )
    def test_stems(self, name: str, stem: str) -> None:
        assert name_stem(name) == stem


@pytest.fixture
async def client(pg_dsn: str):
    enable_socket()
    conn = await asyncpg.connect(pg_dsn)
    try:
        cid = await conn.fetchval(
            "insert into company (cik,ticker,name,watched,next_earnings) "
            "values ('0000000001','FCEL','FuelCell Energy, Inc.',true,'2026-12-10') returning id"
        )
        for value in ("FCEL", "FCELB"):
            await conn.execute(
                "insert into company_alias (company_id,kind,value) values ($1,'ticker',$2)",
                cid,
                value,
            )
        for days, close in [(30, "12.00"), (8, "15.00"), (0, "18.00")]:
            await conn.execute(
                "insert into price_daily (company_id,d,close) values ($1,$2,$3)",
                cid,
                (NOW - timedelta(days=days)).date(),
                Decimal(close),
            )
        events = [
            (
                "edgar_form4",
                "form4_buy",
                65,
                "insider_buy",
                3,
                {
                    "insider_ciks": ["0000000111"],
                    "insider_names": ["Livingston Homer"],
                    "codes": ["P"],
                    "purchase_value": 246955.0,
                    "sale_value": 0.0,
                },
            ),
            (
                "edgar_form4",
                "form4_other",
                0,
                "insider_sell",
                40,
                {
                    "insider_ciks": ["0000000222"],
                    "insider_names": ["Seller Sam"],
                    "codes": ["S"],
                    "purchase_value": 0.0,
                    "sale_value": 90000.0,
                },
            ),
            ("edgar_8k", "8k", 45, "officer_change", 50, {"items": ["5.02"]}),
        ]
        for i, (source, etype, score, cat, days, payload) in enumerate(events):
            await conn.execute(
                """insert into event (company_id, source, event_type, occurred_at, external_id,
                                      summary, score, payload, category, url)
                   values ($1,$2,$3,$4,$5,'s',$6,$7::jsonb,$8,'https://www.sec.gov/x')""",
                cid,
                source,
                etype,
                NOW - timedelta(days=days),
                f"c{i}",
                score,
                json.dumps(payload),
                cat,
            )
        await conn.execute(
            "insert into unresolved (source, external_id, raw_name, payload) "
            "values ('edgar_8k','u1','FUELCELL ENERGY FINANCE LLC','{}')"
        )
    finally:
        await conn.close()

    store = await PgStore.connect(pg_dsn)
    deps.set_store(store)
    deps.set_settings(Settings.from_env({"DATABASE_URL": pg_dsn}, require_sec_user_agent=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as c:
        yield c
    await store.close()
    deps.set_store(None)


@pytest.mark.pg
class TestEndpoint:
    async def test_header(self, client: httpx.AsyncClient) -> None:
        company = (await client.get("/api/company/FCEL")).json()["company"]
        assert company["name"] == "FuelCell Energy, Inc."
        assert company["last_price"] == 18.0
        assert company["week_change"] == pytest.approx(20.0)
        assert company["next_earnings"] == "2026-12-10"
        assert company["flags_this_month"] == 1

    async def test_any_share_class_reaches_the_company(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/company/fcelb")).json()["company"]["ticker"] == "FCEL"

    async def test_unknown_ticker_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/company/NOPE")).status_code == 404

    async def test_events_are_scoped_and_take_the_feeds_filters(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/api/company/FCEL")).json()
        assert body["total"] == 3
        flagged = (await client.get("/api/company/FCEL?min_score=30&range=7d")).json()
        assert [e["category"] for e in flagged["events"]] == ["insider_buy"]

    async def test_prices_are_ordered_for_drawing(self, client: httpx.AsyncClient) -> None:
        prices = (await client.get("/api/company/FCEL")).json()["prices"]
        assert [p["close"] for p in prices] == [12.0, 15.0, 18.0]

    async def test_insiders_show_bought_versus_sold(self, client: httpx.AsyncClient) -> None:
        insiders = (await client.get("/api/company/FCEL")).json()["insiders"]
        assert [(i["name"], i["bought_90d"], i["sold_90d"]) for i in insiders] == [
            ("Livingston Homer", 246955.0, 0.0),
            ("Seller Sam", 0.0, 90000.0),
        ]

    async def test_possible_aliases_surface_unresolved_filings(
        self, client: httpx.AsyncClient
    ) -> None:
        aliases = (await client.get("/api/company/FCEL")).json()["possible_aliases"]
        assert [a["raw_name"] for a in aliases] == ["FUELCELL ENERGY FINANCE LLC"]

    async def test_pagination(self, client: httpx.AsyncClient) -> None:
        page = (await client.get("/api/company/FCEL?limit=1&offset=1")).json()
        assert len(page["events"]) == 1 and page["total"] == 3
