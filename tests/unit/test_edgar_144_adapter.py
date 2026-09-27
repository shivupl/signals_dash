"""Form 144 hydration: the subject rule, the grace timer, and the budget.

Same shape as Form 4 -- a filing appears once per party and the document holds the
detail -- with one difference that matters: the company is the ``(Subject)``
entry, not ``(Issuer)``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from signals.adapters.base import FetchContext
from signals.adapters.edgar_144 import SUBJECT_GRACE_SECONDS, Edgar144Adapter, Form144State
from signals.http import SourceClient
from tests.conftest import read_fixture
from tests.fakes import FakeClock

NOTICE = read_fixture("edgar", "form144_officer_ns2.txt")
UA = "Signals/0.1 (test@example.com)"

# The issuer in form144_officer_ns2.txt.
SUBJECT_CIK = "0001943896"
OTHER_CIK = "0000000099"
SELLER_CIK = "0001685768"


def atom(entries: list[tuple[str, str]]) -> bytes:
    """(title, accession) -> a minimal but real-shaped 144 feed."""
    body = "".join(
        f"""<entry><title>{title}</title>
        <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1/{acc}-index.htm"/>
        <summary type="html">AccNo: {acc}</summary>
        <updated>2026-09-25T19:19:45-04:00</updated>
        <id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>"""
        for title, acc in entries
    )
    return (
        '<?xml version="1.0"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'
    ).encode()


def pair(accession: str, subject_cik: str = SUBJECT_CIK) -> list[tuple[str, str]]:
    return [
        (f"144 - Rubrik, Inc. ({subject_cik}) (Subject)", accession),
        (f"144 - Sinha Bipul ({SELLER_CIK}) (Reporting)", accession),
    ]


class Recorder:
    def __init__(self, index: bytes, submission: bytes = NOTICE) -> None:
        self.index = index
        self.submission = submission
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.urls.append(url)
        if "browse-edgar" in url:
            return httpx.Response(200, content=self.index)
        return httpx.Response(200, content=self.submission)

    @property
    def document_fetches(self) -> int:
        return len([u for u in self.urls if u.endswith(".txt")])


def rig(index: bytes, watched: set[str], submission: bytes = NOTICE):
    recorder = Recorder(index, submission)
    clock = FakeClock()
    ctx = FetchContext(
        http=SourceClient(UA, clock=clock, transport=httpx.MockTransport(recorder)),
        clock=clock,
        watched_ciks=frozenset(watched),
        state={},
    )
    return Edgar144Adapter(), ctx, recorder, clock


class TestSubjectRule:
    async def test_a_watched_subject_is_hydrated(self) -> None:
        adapter, ctx, recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SUBJECT_CIK})
        raws = await adapter.fetch(ctx)
        assert len(raws) == 1
        assert recorder.document_fetches == 1

    async def test_an_unwatched_subject_costs_no_request(self) -> None:
        """The budget rule: ~18 notices a day market-wide, and only a handful are
        about a watched company."""
        adapter, ctx, recorder, _clock = rig(atom(pair("0001958244-26-000624")), {OTHER_CIK})
        assert await adapter.fetch(ctx) == []
        assert recorder.document_fetches == 0

    async def test_the_seller_cik_is_never_mistaken_for_the_company(self) -> None:
        """A (Reporting) CIK belongs to a person. Watching it must not trigger a
        fetch, or every notice would look interesting."""
        adapter, ctx, recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SELLER_CIK})
        assert await adapter.fetch(ctx) == []
        assert recorder.document_fetches == 0

    async def test_the_company_comes_from_the_document(self) -> None:
        adapter, ctx, _recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SUBJECT_CIK})
        raws = await adapter.fetch(ctx)
        events = adapter.normalize(raws[0])
        assert len(events) == 1
        assert events[0].company_key is not None
        assert events[0].company_key.cik == SUBJECT_CIK


class TestPrefixMatch:
    async def test_other_forms_starting_with_144_are_ignored(self) -> None:
        """browse-edgar's type is a prefix match, the trap that cost Form 4 most
        of its resolution rate."""
        feed = atom([("1445 - Something Else (0000000001) (Filer)", "0000000000-26-000001")])
        adapter, ctx, recorder, _clock = rig(feed, {"0000000001"})
        assert await adapter.fetch(ctx) == []
        assert recorder.document_fetches == 0

    async def test_an_amendment_is_accepted(self) -> None:
        feed = [
            (f"144/A - Rubrik, Inc. ({SUBJECT_CIK}) (Subject)", "0001958244-26-000999"),
            (f"144/A - Sinha Bipul ({SELLER_CIK}) (Reporting)", "0001958244-26-000999"),
        ]
        adapter, ctx, _recorder, _clock = rig(atom(feed), {SUBJECT_CIK})
        assert len(await adapter.fetch(ctx)) == 1


class TestGrace:
    async def test_a_lone_reporting_entry_waits_rather_than_guessing(self) -> None:
        """Truncation splits a pair. Attributing the notice to the person would be
        locked in permanently by the unique constraint."""
        feed = atom([(f"144 - Sinha Bipul ({SELLER_CIK}) (Reporting)", "0001958244-26-000624")])
        adapter, ctx, recorder, _clock = rig(feed, {SUBJECT_CIK})
        assert await adapter.fetch(ctx) == []
        assert recorder.document_fetches == 0

    async def test_grace_expiry_hydrates_anyway(self) -> None:
        feed = atom([(f"144 - Sinha Bipul ({SELLER_CIK}) (Reporting)", "0001958244-26-000624")])
        adapter, ctx, recorder, clock = rig(feed, {SUBJECT_CIK})
        assert await adapter.fetch(ctx) == []
        clock.advance(SUBJECT_GRACE_SECONDS + 1)
        ctx.state.pop("digest:144", None)  # the feed is unchanged; force a re-read
        raws = await adapter.fetch(ctx)
        assert len(raws) == 1, "a genuinely split filing must not be lost"
        assert recorder.document_fetches == 1


class TestIdempotence:
    async def test_re_polling_the_same_feed_fetches_nothing_twice(self) -> None:
        adapter, ctx, recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SUBJECT_CIK})
        first = await adapter.fetch(ctx)
        ctx.state.pop("digest:144", None)
        second = await adapter.fetch(ctx)
        assert len(first) == 1
        assert second == []
        assert recorder.document_fetches == 1

    async def test_an_unchanged_feed_is_not_reparsed(self) -> None:
        adapter, ctx, recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SUBJECT_CIK})
        await adapter.fetch(ctx)
        await adapter.fetch(ctx)
        assert len([u for u in recorder.urls if "browse-edgar" in u]) == 2


class TestNormalize:
    async def test_the_payload_carries_what_the_scorer_needs(self) -> None:
        adapter, ctx, _recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SUBJECT_CIK})
        event = adapter.normalize((await adapter.fetch(ctx))[0])[0]
        p = event.payload
        assert event.source == "edgar_144"
        assert event.event_type == "sale_notice"
        assert p["value"] == 56_900_000.0
        assert p["is_insider"] is True
        assert p["plan_10b5_1"] is False
        assert p["seller_ciks"] == [SELLER_CIK], "cluster counting is by CIK"
        assert round(p["percent_outstanding"], 2) == 0.30
        assert p["headline"] == "Planned sale by Bipul Sinha · $56,900,000"

    async def test_the_occurred_at_is_the_acceptance_time(self) -> None:
        adapter, ctx, _recorder, _clock = rig(atom(pair("0001958244-26-000624")), {SUBJECT_CIK})
        event = adapter.normalize((await adapter.fetch(ctx))[0])[0]
        assert event.occurred_at.tzinfo is not None, "naive timestamps are rejected upstream"
        # The instant, not its rendering: the feed states Eastern and the parser
        # keeps the offset it was given.
        assert event.occurred_at == datetime(2026, 9, 25, 23, 19, 45, tzinfo=UTC)


class TestState:
    def test_the_hydrated_set_is_bounded(self) -> None:
        state = Form144State()
        for n in range(6000):
            state.remember(f"acc-{n}")
        assert len(state.hydrated) <= 5000


class TestMissingIssuerInTheDocument:
    async def test_falls_back_to_the_index_subject(self) -> None:
        """No sampled filing omitted issuerCik, but dropping a notice over a
        missing field would be worse than using the CIK the index already gave."""
        stripped = NOTICE.replace(b"<ns2:issuerCik>0001943896</ns2:issuerCik>", b"")
        adapter, ctx, _recorder, _clock = rig(
            atom(pair("0001958244-26-000624")), {SUBJECT_CIK}, submission=stripped
        )
        raws = await adapter.fetch(ctx)
        assert len(raws) == 1
        event = adapter.normalize(raws[0])[0]
        assert event.company_key is not None
        assert event.company_key.cik == SUBJECT_CIK
