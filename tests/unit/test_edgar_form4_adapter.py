"""Form 4 hydration: the budget, the grace timer, and the issuer rule."""

from __future__ import annotations

import httpx

from signals.adapters.base import FetchContext
from signals.adapters.edgar_form4 import (
    ISSUER_GRACE_SECONDS,
    EdgarForm4Adapter,
    Form4State,
    submission_url,
)
from signals.http import SourceClient
from tests.conftest import read_fixture
from tests.fakes import FakeClock

PURCHASE = read_fixture("edgar", "form4_purchase_P.txt")
AWARD = read_fixture("edgar", "form4_award_only.txt")
UA = "Signals/0.1 (test@example.com)"

ISSUER_CIK = "0000000042"
OTHER_CIK = "0000000099"


def atom(entries: list[tuple[str, str, str]]) -> bytes:
    """(form, name+cik+role string, accession) -> a minimal but real-shaped feed."""
    body = "".join(
        f"""<entry><title>{title}</title>
        <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1/{acc}-index.htm"/>
        <summary type="html">AccNo: {acc}</summary>
        <updated>2026-09-15T21:57:13-04:00</updated>
        <id>urn:tag:sec.gov,2008:accession-number={acc}</id></entry>"""
        for _form, title, acc in entries
    )
    return (
        '<?xml version="1.0"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'
    ).encode()


class Recorder:
    """Serves the index then the submission, and records what was requested."""

    def __init__(self, index: bytes, submission: bytes = PURCHASE) -> None:
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


def make_ctx(recorder: Recorder, clock: FakeClock, watched: set[str]) -> FetchContext:
    return FetchContext(
        http=SourceClient(UA, transport=httpx.MockTransport(recorder), clock=clock),
        clock=clock,
        watched_ciks=frozenset(watched),
    )


ACC = "0001234567-26-000001"
WITH_ISSUER = [
    ("4", f"4 - Jane Insider ({OTHER_CIK}) (Reporting)", ACC),
    ("4", f"4 - Watched Corp ({ISSUER_CIK}) (Issuer)", ACC),
]
WITHOUT_ISSUER = [("4", f"4 - Jane Insider ({OTHER_CIK}) (Reporting)", ACC)]


class TestSubmissionUrl:
    def test_derives_the_full_submission_from_the_index_link(self) -> None:
        """One request instead of two: the XML document's filename is not
        predictable, so fetching it by name would need a directory listing."""
        url = submission_url(
            "https://www.sec.gov/Archives/edgar/data/1/000123-index.htm", "0001-26-1"
        )
        assert url == "https://www.sec.gov/Archives/edgar/data/1/0001-26-1.txt"

    def test_no_link_means_no_url(self) -> None:
        assert submission_url(None, "x") is None


class TestHydrationBudget:
    async def test_a_watched_issuer_is_hydrated(self) -> None:
        rec = Recorder(atom(WITH_ISSUER))
        adapter = EdgarForm4Adapter()
        raws = await adapter.fetch(make_ctx(rec, FakeClock(), {ISSUER_CIK}))
        assert len(raws) == 1
        assert rec.document_fetches == 1

    async def test_an_unwatched_issuer_costs_no_request(self) -> None:
        """Hydrating every Form 4 on EDGAR is thousands of requests a day, and
        after 16:00 ET a burst of them."""
        rec = Recorder(atom(WITH_ISSUER))
        adapter = EdgarForm4Adapter()
        raws = await adapter.fetch(make_ctx(rec, FakeClock(), {"0000000777"}))
        assert raws == []
        assert rec.document_fetches == 0

    async def test_a_filing_is_hydrated_only_once(self) -> None:
        """The same filing stays in the window for many polls; re-fetching its
        document on each would waste the entire request budget."""
        rec = Recorder(atom(WITH_ISSUER))
        clock = FakeClock()
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, clock, {ISSUER_CIK})

        await adapter.fetch(ctx)
        ctx.state.pop("digest:4")  # force a re-parse, as a changed feed would
        await adapter.fetch(ctx)

        assert rec.document_fetches == 1

    async def test_an_unchanged_feed_is_not_reparsed(self) -> None:
        rec = Recorder(atom(WITH_ISSUER))
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, FakeClock(), {ISSUER_CIK})
        first = await adapter.fetch(ctx)
        second = await adapter.fetch(ctx)
        assert len(first) == 1
        assert second == []


class TestGraceTimer:
    async def test_an_issuerless_filing_waits_rather_than_guessing(self) -> None:
        """Attributing it to the reporting insider would be locked in by the
        unique constraint, and an insider is never a watchlist company."""
        rec = Recorder(atom(WITHOUT_ISSUER))
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, FakeClock(), {ISSUER_CIK})

        raws = await adapter.fetch(ctx)

        assert raws == []
        assert rec.document_fetches == 0
        assert ctx.state["form4"].counters.waiting_for_issuer == 1

    async def test_the_sibling_arriving_next_poll_resolves_it_normally(self) -> None:
        rec = Recorder(atom(WITHOUT_ISSUER))
        clock = FakeClock()
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, clock, {ISSUER_CIK})

        await adapter.fetch(ctx)
        rec.index = atom(WITH_ISSUER)  # the issuer entry shows up
        ctx.state.pop("digest:4")
        raws = await adapter.fetch(ctx)

        assert len(raws) == 1
        assert ctx.state["form4"].counters.grace_expired == 0

    async def test_grace_expiry_fetches_the_document_anyway(self) -> None:
        """The backstop: a genuinely split filing must not be lost."""
        rec = Recorder(atom(WITHOUT_ISSUER))
        clock = FakeClock()
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, clock, {ISSUER_CIK})

        await adapter.fetch(ctx)
        clock.advance(ISSUER_GRACE_SECONDS + 1)
        ctx.state.pop("digest:4")
        raws = await adapter.fetch(ctx)

        assert len(raws) == 1
        assert ctx.state["form4"].counters.grace_expired == 1
        assert rec.document_fetches == 1

    async def test_the_issuer_comes_from_the_document_after_grace(self) -> None:
        rec = Recorder(atom(WITHOUT_ISSUER))
        clock = FakeClock()
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, clock, {ISSUER_CIK})
        await adapter.fetch(ctx)
        clock.advance(ISSUER_GRACE_SECONDS + 1)
        ctx.state.pop("digest:4")

        event = adapter.normalize((await adapter.fetch(ctx))[0])[0]

        assert event.company_key.cik is not None
        assert event.company_key.cik != OTHER_CIK, "resolved to the insider"


class TestIssuerAuthority:
    async def test_the_document_overrides_a_disagreeing_index(self) -> None:
        """If these ever disagree, the title parser is wrong -- and the document
        is the one EDGAR actually validated."""
        rec = Recorder(atom(WITH_ISSUER))
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, FakeClock(), {ISSUER_CIK})
        raws = await adapter.fetch(ctx)

        event = adapter.normalize(raws[0])[0]
        doc = raws[0].payload["doc"]

        assert event.company_key.cik == doc.issuer_cik
        assert ctx.state["form4"].counters.hint_mismatch == 1  # fixture is a real filing


class TestNormalize:
    async def _event(self, submission: bytes):
        rec = Recorder(atom(WITH_ISSUER), submission=submission)
        adapter = EdgarForm4Adapter()
        raws = await adapter.fetch(make_ctx(rec, FakeClock(), {ISSUER_CIK}))
        return adapter.normalize(raws[0])[0]

    async def test_a_purchase_is_typed_as_a_buy(self) -> None:
        event = await self._event(PURCHASE)
        assert event.event_type == "form4_buy"
        assert event.payload["is_open_market_purchase"] is True

    async def test_an_award_is_not_typed_as_a_buy(self) -> None:
        event = await self._event(AWARD)
        assert event.event_type == "form4_other"
        assert event.payload["is_open_market_purchase"] is False

    async def test_insider_ciks_are_carried_for_cluster_detection(self) -> None:
        event = await self._event(PURCHASE)
        assert event.payload["insider_ciks"]

    async def test_the_scoring_inputs_are_all_present(self) -> None:
        """Step 5 scores from the payload, so everything it needs is recorded
        now rather than requiring a re-fetch later."""
        payload = (await self._event(PURCHASE)).payload
        for key in (
            "purchase_value", "purchase_shares", "shares_after",
            "plan_10b5_1", "is_officer", "is_director", "officer_title",
        ):
            assert key in payload

    async def test_occurred_at_stays_the_acceptance_time(self) -> None:
        """Hydration must not move the latency baseline to the document fetch."""
        event = await self._event(PURCHASE)
        assert event.occurred_at.tzinfo is not None
        assert event.occurred_at.isoformat().startswith("2026-09-15T21:57:13")


class TestFailureHandling:
    async def test_a_failed_document_fetch_drops_that_filing_only(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "browse-edgar" in str(request.url):
                return httpx.Response(200, content=atom(WITH_ISSUER))
            return httpx.Response(500)

        clock = FakeClock()
        ctx = FetchContext(
            http=SourceClient(UA, transport=httpx.MockTransport(handler), clock=clock),
            clock=clock,
            watched_ciks=frozenset({ISSUER_CIK}),
        )
        raws = await EdgarForm4Adapter().fetch(ctx)
        assert raws == []
        assert ctx.state["form4"].counters.fetch_failures == 1

    async def test_an_unparseable_document_is_counted_not_raised(self) -> None:
        rec = Recorder(atom(WITH_ISSUER), submission=b"<XML><notOwnership/></XML>")
        adapter = EdgarForm4Adapter()
        ctx = make_ctx(rec, FakeClock(), {ISSUER_CIK})
        assert await adapter.fetch(ctx) == []
        assert ctx.state["form4"].counters.parse_failures == 1


class TestStateBounds:
    def test_pending_is_capped(self) -> None:
        """A long run must not grow the scratch dicts without bound."""
        from signals.adapters.edgar_form4 import MAX_PENDING

        state = Form4State()
        for i in range(MAX_PENDING + 500):
            state.wait(f"acc-{i}", float(i))
        assert len(state.pending) <= MAX_PENDING

    def test_hydrated_is_capped(self) -> None:
        from signals.adapters.edgar_form4 import MAX_HYDRATED

        state = Form4State()
        for i in range(MAX_HYDRATED + 500):
            state.remember(f"acc-{i}")
        assert len(state.hydrated) <= MAX_HYDRATED
