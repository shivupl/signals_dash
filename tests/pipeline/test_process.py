"""The process step: resolve, filter to watched, score, store, publish."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from signals.bus.memory_bus import MemoryBus
from signals.models import Company, CompanyKey, NormalizedEvent
from signals.pipeline.process import Processor
from signals.resolve.resolver import Resolver

NOW = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)


def an_event(cik: str = "0000000001", items: list[str] | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        source="edgar_8k",
        external_id=f"acc-{cik}-{','.join(items or [])}",
        event_type="8k",
        occurred_at=NOW,
        company_key=CompanyKey(cik=cik),
        summary="test",
        payload={"items": items or ["4.02"]},
    )


class StubStore:
    """Enough Store to exercise the processor without Postgres."""

    def __init__(self, companies: dict[str, Company] | None = None) -> None:
        self.companies = companies or {}
        self.inserted: list[Any] = []
        self.unresolved: list[tuple[str, str]] = []
        self._unresolved_keys: set[tuple[str, str]] = set()
        self._next_id = 1
        self._seen: set[tuple[str, str]] = set()

    async def find_company(self, key: CompanyKey) -> Company | None:
        return self.companies.get(key.cik or "")

    async def record_unresolved(
        self, source: str, external_id: str, raw_name: str, payload: dict
    ) -> None:
        # Keyed like the real table, so the test sees dedupe rather than growth.
        if (source, external_id) not in self._unresolved_keys:
            self._unresolved_keys.add((source, external_id))
            self.unresolved.append((source, raw_name))

    async def insert_event(self, event: Any) -> int | None:
        key = (event.source, event.external_id)
        if key in self._seen:
            return None
        self._seen.add(key)
        self.inserted.append(event)
        self._next_id += 1
        return self._next_id - 1

    async def distinct_p_buyers(self, company_id: int, since: Any) -> int:
        return 0

    async def events_missing_cluster_bonus(self, company_id: int, since: Any) -> list:
        return []

    async def get_event(self, event_id: int) -> Any:
        from signals.store.base import EventRow

        event = self.inserted[event_id - 1]
        return EventRow(
            id=event_id,
            company_id=event.company_id,
            source=event.source,
            event_type=event.normalized.event_type,
            occurred_at=event.occurred_at,
            ingested_at=NOW,
            external_id=event.external_id,
            summary=event.normalized.summary,
            score=event.score.total,
            price_at=None,
            payload={"score_parts": event.score.parts},
            url=None,
        )


WATCHED = Company(id=1, cik="0000000001", ticker="AAA", name="Watched Co", watched=True)
UNWATCHED = Company(id=2, cik="0000000002", ticker="BBB", name="Other Co", watched=False)


@pytest.fixture
def setup() -> tuple[StubStore, MemoryBus, Processor]:
    store = StubStore({"0000000001": WATCHED, "0000000002": UNWATCHED})
    bus = MemoryBus()
    processor = Processor(
        store=store,  # type: ignore[arg-type]
        resolver=Resolver(store),  # type: ignore[arg-type]
        publisher=bus,
        flag_threshold=30,
    )
    return store, bus, processor


class TestWatchedFilter:
    async def test_a_watched_company_is_stored(self, setup) -> None:
        store, _bus, processor = setup
        stats = await processor.process(an_event("0000000001"))
        assert stats.stored == 1
        assert len(store.inserted) == 1

    async def test_an_unwatched_company_is_dropped_before_the_insert(self, setup) -> None:
        """EDGAR publishes thousands of filings a day from companies nobody here
        watches. Storing them all is what makes the database slow."""
        store, _bus, processor = setup
        stats = await processor.process(an_event("0000000002"))
        assert stats.skipped_unwatched == 1
        assert store.inserted == []

    async def test_an_unknown_company_goes_to_the_unresolved_queue(self, setup) -> None:
        store, _bus, processor = setup
        stats = await processor.process(an_event("0000009999"))
        assert stats.resolved == 0
        assert store.unresolved == [("edgar_8k", "0000009999")]

    async def test_unresolved_is_recorded_even_though_nothing_is_stored(self, setup) -> None:
        """A work list, not a bin: it is how you find out a source names
        companies in a way resolution does not understand yet."""
        store, _bus, processor = setup
        await processor.process(an_event("0000009999"))
        assert len(store.unresolved) == 1


class TestDedupe:
    async def test_the_same_filing_twice_stores_once(self, setup) -> None:
        store, _bus, processor = setup
        event = an_event("0000000001")
        first = await processor.process(event)
        second = await processor.process(event)
        assert (first.stored, first.duplicate) == (1, 0)
        assert (second.stored, second.duplicate) == (0, 1)
        assert len(store.inserted) == 1

    async def test_a_duplicate_is_not_republished(self, setup) -> None:
        """Re-polling the index must not re-flag a filing you already read."""
        _store, bus, processor = setup
        event = an_event("0000000001")
        await processor.process(event)
        await processor.process(event)
        assert len(bus.published) == 1


class TestPublishing:
    async def test_publishes_above_the_threshold(self, setup) -> None:
        _store, bus, processor = setup
        await processor.process(an_event("0000000001", ["4.02"]))  # 95
        assert [m.type for m in bus.published] == ["event.new"]

    async def test_stores_but_does_not_publish_below_the_threshold(self, setup) -> None:
        """Sub-threshold events must still be stored, or cluster promotion could
        never find and lift one later."""
        store, bus, processor = setup
        await processor.process(an_event("0000000001", ["9.01"]))  # routine, < 30
        assert len(store.inserted) == 1
        assert bus.published == []

    async def test_the_message_carries_the_whole_event(self, setup) -> None:
        """Never a patch: a client filtering by score may not hold this event at
        all, so it has to be able to insert from the message."""
        _store, bus, processor = setup
        await processor.process(an_event("0000000001", ["4.02"]))
        data = bus.published[0].data
        assert data["score"] == 95
        assert data["tier"] == "critical"
        assert "headline" in data


class TestScoring:
    @pytest.mark.parametrize(
        ("items", "expected"),
        [(["4.02"], 95), (["3.01"], 85), (["2.02"], 60), (["5.02"], 45)],
    )
    async def test_scores_are_applied_on_the_way_in(self, setup, items, expected) -> None:
        store, _bus, processor = setup
        await processor.process(an_event("0000000001", items))
        assert store.inserted[0].score.total == expected


class TestLiveCaptureMarking:
    """Latency must only count filings the worker was running to see.

    The first poll of a 100-entry window returns filings accepted hours earlier.
    Measuring those reports the age of the backlog, and a dashboard that shows it
    is stating a number known to be wrong.
    """

    def _processor(self, store: StubStore, bus: MemoryBus, started_at):
        return Processor(
            store=store,  # type: ignore[arg-type]
            resolver=Resolver(store),  # type: ignore[arg-type]
            publisher=bus,
            flag_threshold=30,
            started_at=started_at,
        )

    async def test_a_filing_accepted_after_startup_is_live(self) -> None:
        from datetime import timedelta

        store = StubStore({"0000000001": WATCHED})
        processor = self._processor(store, MemoryBus(), NOW - timedelta(minutes=5))
        await processor.process(an_event("0000000001"))
        assert store.inserted[0].normalized.payload["live_capture"] is True

    async def test_a_backfilled_filing_is_not_live(self) -> None:
        from datetime import timedelta

        store = StubStore({"0000000001": WATCHED})
        processor = self._processor(store, MemoryBus(), NOW + timedelta(minutes=5))
        await processor.process(an_event("0000000001"))
        assert store.inserted[0].normalized.payload["live_capture"] is False

    async def test_without_a_start_time_nothing_claims_to_be_live(self) -> None:
        """Replay has no start time, and replayed events must never reach the
        latency figures."""
        store = StubStore({"0000000001": WATCHED})
        processor = self._processor(store, MemoryBus(), None)
        await processor.process(an_event("0000000001"))
        assert store.inserted[0].normalized.payload["live_capture"] is False

    async def test_marking_does_not_disturb_the_rest_of_the_payload(self) -> None:
        store = StubStore({"0000000001": WATCHED})
        processor = self._processor(store, MemoryBus(), NOW)
        await processor.process(an_event("0000000001", ["4.02"]))
        payload = store.inserted[0].normalized.payload
        assert payload["items"] == ["4.02"]
        assert store.inserted[0].score.total == 95


class TestUnresolvedIsAWorkList:
    async def test_the_same_filing_is_recorded_once(self, setup) -> None:
        """Without a key this re-recorded on every poll: 16 real entities became
        6,668 rows in four hours."""
        store, _bus, processor = setup
        event = an_event("0000009999")
        await processor.process(event)
        await processor.process(event)
        await processor.process(event)
        assert len(store.unresolved) == 1

    async def test_different_filings_are_recorded_separately(self, setup) -> None:
        store, _bus, processor = setup
        await processor.process(an_event("0000009999", ["1.01"]))
        await processor.process(an_event("0000009998", ["2.02"]))
        assert len(store.unresolved) == 2
