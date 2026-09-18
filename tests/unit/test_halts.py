"""Halts: parser, scoring, and the adapter's state tracking."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import httpx
import pytest

from signals.adapters.base import FetchContext
from signals.adapters.halts import HaltsAdapter, halt_instant, prior_pauses, resume_instant
from signals.http import SourceClient
from signals.parsers.halts_rss import parse_halts
from signals.scoring.halts import describe_halt, score_halt, score_resume
from tests.conftest import read_fixture
from tests.fakes import FakeClock

REAL = read_fixture("halts", "tradehalts_current.xml")
UA = "Signals/0.1 (test@example.com)"


def feed(items: list[dict[str, str]]) -> bytes:
    def item(d: dict[str, str]) -> str:
        g = lambda k: d.get(k, "")  # noqa: E731
        return f"""<item><title>{g('sym')}</title>
        <ndaq:HaltDate>{g('date') or '09/15/2026'}</ndaq:HaltDate>
        <ndaq:HaltTime>{g('time') or '09:41:00.000'}</ndaq:HaltTime>
        <ndaq:IssueSymbol>{g('sym')}</ndaq:IssueSymbol>
        <ndaq:IssueName>{g('sym')} Corp</ndaq:IssueName>
        <ndaq:Market>{g('market') or 'NASDAQ'}</ndaq:Market>
        <ndaq:ReasonCode>{g('reason') or 'T1'}</ndaq:ReasonCode>
        <ndaq:PauseThresholdPrice />
        <ndaq:ResumptionDate>{g('rdate')}</ndaq:ResumptionDate>
        <ndaq:ResumptionQuoteTime>{g('quote')}</ndaq:ResumptionQuoteTime>
        <ndaq:ResumptionTradeTime>{g('trade')}</ndaq:ResumptionTradeTime></item>"""

    body = "".join(item(i) for i in items)
    return (
        b"\xef\xbb\xbf"
        + f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0" '
        f'xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>{body}</channel></rss>'.encode()
    )


def ctx_for(holder: dict[str, bytes], watched: set[str] | None = None) -> FetchContext:
    clock = FakeClock()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=holder["body"]))
    return FetchContext(
        http=SourceClient(UA, transport=transport, clock=clock),
        clock=clock,
        watched_tickers=frozenset(watched or ()),
    )


class TestParser:
    def test_parses_a_real_capture_with_its_byte_order_mark(self) -> None:
        """The live feed opens with a UTF-8 BOM, which lxml rejects before an
        XML declaration."""
        assert REAL.startswith(b"\xef\xbb\xbf")
        assert len(parse_halts(REAL)) > 100

    def test_covers_more_than_nasdaq_listings(self) -> None:
        """Why this feed replaced the exchange page in the original design."""
        markets = {i.market for i in parse_halts(REAL)}
        assert "NASDAQ" in markets
        assert markets & {"NYSE", "NYSE Arca", "AMEX"}

    def test_both_time_formats_parse(self) -> None:
        """HH:MM:SS and HH:MM:SS.mmm appear in the same response."""
        items = parse_halts(feed([{"sym": "A", "time": "09:41:00"},
                                  {"sym": "B", "time": "09:41:00.123"}]))
        assert [i.halt_time for i in items] == [time(9, 41), time(9, 41, 0, 123000)]

    def test_states(self) -> None:
        items = parse_halts(feed([
            {"sym": "OPEN"},
            {"sym": "QUOT", "rdate": "09/15/2026", "quote": "10:00:00"},
            {"sym": "DONE", "rdate": "09/15/2026", "quote": "10:00:00", "trade": "10:05:00"},
        ]))
        assert [i.state for i in items] == ["open", "quoting", "resumed"]

    def test_the_key_survives_the_item_mutating(self) -> None:
        before = parse_halts(feed([{"sym": "X"}]))[0]
        after = parse_halts(feed([{"sym": "X", "rdate": "09/15/2026", "trade": "10:05:00"}]))[0]
        assert before.natural_key == after.natural_key

    def test_items_without_a_symbol_or_time_are_skipped(self) -> None:
        assert parse_halts(feed([{"sym": ""}, {"sym": "OK"}]))[0].symbol == "OK"

    def test_an_empty_feed_is_normal(self) -> None:
        assert parse_halts(feed([])) == []


class TestTimezones:
    def test_halt_time_is_eastern_converted_to_utc(self) -> None:
        item = parse_halts(feed([{"sym": "X", "date": "09/15/2026", "time": "09:41:00"}]))[0]
        assert halt_instant(item) == datetime(2026, 9, 15, 13, 41, tzinfo=UTC)

    def test_winter_offset_differs(self) -> None:
        item = parse_halts(feed([{"sym": "X", "date": "12/15/2026", "time": "09:41:00"}]))[0]
        assert halt_instant(item) == datetime(2026, 12, 15, 14, 41, tzinfo=UTC)

    def test_a_halt_inside_the_repeated_fall_back_hour_resolves(self) -> None:
        item = parse_halts(feed([{"sym": "X", "date": "11/01/2026", "time": "01:30:00"}]))[0]
        assert halt_instant(item) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

    def test_resume_prefers_trade_time_and_falls_back_to_quote(self) -> None:
        quoting = parse_halts(feed([{"sym": "X", "rdate": "09/15/2026", "quote": "10:00:00"}]))[0]
        resumed = parse_halts(feed([{"sym": "X", "rdate": "09/15/2026",
                                     "quote": "10:00:00", "trade": "10:05:00"}]))[0]
        assert resume_instant(quoting) == datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
        assert resume_instant(resumed) == datetime(2026, 9, 15, 14, 5, tzinfo=UTC)


class TestScoring:
    @pytest.mark.parametrize(
        ("code", "score"),
        [("T1", 90), ("H10", 85), ("T12", 80), ("T3", 45), ("T2", 40),
         ("LUDP", 30), ("M", 30), ("MWC1", 50), ("ZZZ", 25)],
    )
    def test_codes(self, code: str, score: int) -> None:
        assert score_halt(code).total == score

    def test_m_is_a_volatility_pause_not_a_circuit_breaker(self) -> None:
        """The original design had this wrong. In live data M is a per-stock
        five-minute pause on NYSE/Arca/AMEX names; only MWC1-3 are market-wide."""
        from signals.scoring.tables import MARKET_WIDE_REASONS, VOLATILITY_REASONS

        assert "M" in VOLATILITY_REASONS
        assert "M" not in MARKET_WIDE_REASONS

    def test_repeat_pauses_fall_below_the_flag_threshold(self) -> None:
        """One symbol in the captured feed was paused 31 times in a day. At 30
        apiece that single ticker would exhaust the daily flag budget."""
        assert score_halt("LUDP", prior_pauses_today=0).total == 30
        assert score_halt("LUDP", prior_pauses_today=1).total < 30

    def test_news_halts_are_never_dampened(self) -> None:
        assert score_halt("T1", prior_pauses_today=5).total == 90

    def test_resume_is_recorded_but_never_flags(self) -> None:
        assert score_resume().total == 10

    def test_describe(self) -> None:
        headline, detail = describe_halt("T1", "NASDAQ", "10:05")
        assert headline == "Halted, news pending"
        assert detail == "nasdaq · code T1 · resumes 10:05"


class TestPriorPauses:
    def test_counts_earlier_pauses_for_the_same_symbol_and_day(self) -> None:
        items = parse_halts(feed([
            {"sym": "THIN", "reason": "LUDP", "time": "09:35:00"},
            {"sym": "THIN", "reason": "LUDP", "time": "09:45:00"},
            {"sym": "THIN", "reason": "LUDP", "time": "09:55:00"},
            {"sym": "OTHER", "reason": "LUDP", "time": "09:40:00"},
        ]))
        counts = prior_pauses(items)
        by_time = {i.halt_time: counts[i.natural_key] for i in items if i.symbol == "THIN"}
        assert by_time == {time(9, 35): 0, time(9, 45): 1, time(9, 55): 2}

    def test_the_real_feed_has_heavy_repeaters(self) -> None:
        assert max(prior_pauses(parse_halts(REAL)).values()) >= 5

    def test_a_new_day_starts_the_count_again(self) -> None:
        items = parse_halts(feed([
            {"sym": "X", "reason": "LUDP", "date": "09/15/2026"},
            {"sym": "X", "reason": "LUDP", "date": "09/16/2026"},
        ]))
        assert set(prior_pauses(items).values()) == {0}
        assert items[0].halt_date == date(2026, 9, 15)


class TestAdapterLifecycle:
    async def test_an_open_halt_emits_the_halt_only(self) -> None:
        holder = {"body": feed([{"sym": "RKLB"}])}
        adapter, ctx = HaltsAdapter(), ctx_for(holder)
        raws = await adapter.fetch(ctx)
        events = adapter.normalize(raws[0])
        assert [e.event_type for e in events] == ["halt"]
        assert events[0].external_id.startswith("halt:RKLB|")

    async def test_re_polling_an_unchanged_halt_emits_nothing(self) -> None:
        holder = {"body": feed([{"sym": "RKLB"}])}
        adapter, ctx = HaltsAdapter(), ctx_for(holder)
        await adapter.fetch(ctx)
        ctx.state.pop("digest")
        assert await adapter.fetch(ctx) == []

    async def test_resumption_emits_halt_and_resume_under_distinct_ids(self) -> None:
        """The same feed item, mutated. A shared id would make the resume a
        duplicate that ON CONFLICT silently swallows."""
        holder = {"body": feed([{"sym": "RKLB"}])}
        adapter, ctx = HaltsAdapter(), ctx_for(holder)
        await adapter.fetch(ctx)
        holder["body"] = feed([{"sym": "RKLB", "rdate": "09/15/2026", "trade": "10:05:00"}])
        raws = await adapter.fetch(ctx)
        events = adapter.normalize(raws[0])
        assert [e.event_type for e in events] == ["halt", "halt_resume"]
        assert events[0].external_id != events[1].external_id
        assert events[1].external_id.startswith("resume:RKLB|")

    async def test_the_resume_id_does_not_move_when_trading_follows_quotes(self) -> None:
        holder = {"body": feed([{"sym": "X", "rdate": "09/15/2026", "quote": "10:00:00"}])}
        adapter, ctx = HaltsAdapter(), ctx_for(holder)
        first = adapter.normalize((await adapter.fetch(ctx))[0])[1]
        holder["body"] = feed([{"sym": "X", "rdate": "09/15/2026",
                                "quote": "10:00:00", "trade": "10:05:00"}])
        second = adapter.normalize((await adapter.fetch(ctx))[0])[1]
        assert first.external_id == second.external_id
        assert first.payload["resumption"]["trading"] is False
        assert second.payload["resumption"]["trading"] is True

    async def test_cold_start_on_an_already_resumed_halt_yields_both(self) -> None:
        holder = {"body": feed([{"sym": "X", "rdate": "09/15/2026", "trade": "10:05:00"}])}
        adapter, ctx = HaltsAdapter(), ctx_for(holder)
        events = adapter.normalize((await adapter.fetch(ctx))[0])
        assert len(events) == 2
        assert events[0].occurred_at < events[1].occurred_at

    async def test_unwatched_symbols_are_dropped_before_anything_else(self) -> None:
        holder = {"body": feed([{"sym": "RKLB"}, {"sym": "NOPE"}])}
        adapter, ctx = HaltsAdapter(), ctx_for(holder, watched={"RKLB"})
        raws = await adapter.fetch(ctx)
        assert [r.payload["item"].symbol for r in raws] == ["RKLB"]

    async def test_a_market_wide_halt_collapses_to_one_row_and_skips_the_filter(self) -> None:
        holder = {
            "body": feed([{"sym": "AAA", "reason": "MWC1"}, {"sym": "BBB", "reason": "MWC1"}])
        }
        adapter, ctx = HaltsAdapter(), ctx_for(holder, watched={"ZZZ"})
        raws = await adapter.fetch(ctx)
        assert len(raws) == 1
        event = adapter.normalize(raws[0])[0]
        assert event.payload["market_wide"] is True
        assert not event.company_key

    async def test_not_gated_to_market_hours(self) -> None:
        """News halts land pre-market, which is when they matter most."""
        assert HaltsAdapter.market_hours_only is False
