"""The EDGAR index adapter, driven by a mock transport over real fixtures."""

from __future__ import annotations

import httpx
import pytest

from signals.adapters.base import FetchContext
from signals.adapters.edgar_index import (
    EdgarIndexAdapter,
    build_edgar_adapters,
    index_url,
    normalize_form4,
)
from signals.http import SourceClient
from signals.parsers.edgar_atom import group_by_accession, parse_atom
from signals.parsers.edgar_titles import Role
from tests.conftest import read_fixture
from tests.fakes import FakeClock

FORM4 = read_fixture("edgar", "atom_form4_current.xml")
EIGHTK = read_fixture("edgar", "atom_8k_current.xml")
EMPTY = read_fixture("edgar", "atom_empty_feed.xml")
UA = "Signals/0.1 (test@example.com)"


def ctx_for(body: bytes, clock: FakeClock | None = None) -> FetchContext:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    clock = clock or FakeClock()
    return FetchContext(http=SourceClient(UA, transport=transport, clock=clock), clock=clock)


class TestIndexUrl:
    def test_encodes_the_space_in_a_schedule_form(self) -> None:
        """"SC 13D" unencoded silently returns the wrong feed."""
        assert "SC%2013D" in index_url("SC 13D")

    def test_requests_a_full_window(self) -> None:
        assert "count=100" in index_url("8-K")

    def test_asks_for_atom(self) -> None:
        assert "output=atom" in index_url("4")


class TestFetch:
    async def test_yields_one_raw_event_per_filing_not_per_entry(self) -> None:
        """100 atom entries collapse to the filings they belong to -- a joint
        Form 4 can spread one accession across eleven entries."""
        adapter = EdgarIndexAdapter("edgar_form4", ["4"])
        ctx = ctx_for(FORM4)
        raws = await adapter.fetch(ctx)
        assert 0 < len(raws) < 100

    async def test_external_id_is_the_accession(self) -> None:
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"])
        raws = await adapter.fetch(ctx_for(EIGHTK))
        assert all(r.external_id.count("-") == 2 for r in raws)

    async def test_ids_are_unique_within_one_fetch(self) -> None:
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"])
        raws = await adapter.fetch(ctx_for(EIGHTK))
        assert len({r.external_id for r in raws}) == len(raws)

    async def test_an_empty_feed_is_not_an_error(self) -> None:
        """The ordinary state outside filing hours."""
        adapter = EdgarIndexAdapter("edgar_13dg", ["SC 13D"])
        assert await adapter.fetch(ctx_for(EMPTY)) == []

    async def test_an_unchanged_feed_is_skipped_on_the_next_poll(self) -> None:
        """The index is re-served unchanged most of the time; re-parsing it is
        wasted CPU. The request was already spent, so this saves work, not calls."""
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"])
        ctx = ctx_for(EIGHTK)
        first = await adapter.fetch(ctx)
        second = await adapter.fetch(ctx)
        assert len(first) > 0
        assert second == []


class TestNormalize8k:
    async def test_produces_scoreable_events(self) -> None:
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"])
        raws = await adapter.fetch(ctx_for(EIGHTK))
        events = [e for r in raws for e in adapter.normalize(r)]
        assert events
        assert all(e.source == "edgar_8k" for e in events)
        assert any(e.payload["items"] for e in events)

    async def test_occurred_at_is_aware(self) -> None:
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"])
        raws = await adapter.fetch(ctx_for(EIGHTK))
        events = [e for r in raws for e in adapter.normalize(r)]
        assert all(e.occurred_at.tzinfo is not None for e in events)

    async def test_carries_headline_and_detail(self) -> None:
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"])
        raws = await adapter.fetch(ctx_for(EIGHTK))
        event = adapter.normalize(raws[0])[0]
        assert event.payload["headline"]
        assert "edgar" in event.payload["detail"]


class TestForm4Resolution:
    """The Reporting/Issuer rule -- the subtlest thing in this adapter."""

    def test_resolves_to_the_issuer_never_a_reporting_owner(self) -> None:
        groups = [g for g in group_by_accession(parse_atom(FORM4)) if g.has_issuer]
        assert groups, "fixture has no group with an issuer entry"
        for group in groups:
            event = normalize_form4(group)
            assert event.company_key.cik == group.issuer_hint
            assert event.company_key.cik not in group.reporting_ciks

    def test_a_group_without_an_issuer_entry_resolves_to_nothing(self) -> None:
        """Window truncation splits a filing. Attributing it to the insider would
        be worse than leaving it unresolved -- a person is never a watchlist
        company, and the unique constraint would lock the mistake in."""
        groups = [g for g in group_by_accession(parse_atom(FORM4)) if not g.has_issuer]
        assert groups, "fixture happened to contain no split group"
        for group in groups:
            assert normalize_form4(group).company_key.cik is None

    def test_a_joint_filing_records_every_insider(self) -> None:
        """Cluster detection counts distinct insiders by CIK, so all of them have
        to be carried on the event."""
        groups = group_by_accession(parse_atom(FORM4))
        biggest = max(groups, key=lambda g: len(g.reporting_ciks))
        event = normalize_form4(biggest)
        assert len(event.payload["insider_ciks"]) == len(biggest.reporting_ciks)
        assert len(event.payload["insider_ciks"]) > 1

    def test_scores_zero_until_the_xml_is_parsed(self) -> None:
        """The transaction code is the entire signal and it is not in the index.
        Zero is the honest answer, not a placeholder."""
        from signals.scoring import score_event

        group = group_by_accession(parse_atom(FORM4))[0]
        assert score_event(normalize_form4(group)).total == 0

    def test_issuer_entries_are_genuinely_present_in_live_data(self) -> None:
        roles = [e.role for e in parse_atom(FORM4)]
        assert Role.ISSUER in roles
        assert Role.REPORTING in roles


class TestBuildAdapters:
    def test_builds_the_three_edgar_sources(self) -> None:
        names = {a.name for a in build_edgar_adapters()}
        assert names == {"edgar_8k", "edgar_form4", "edgar_13dg"}

    def test_thirteen_d_and_g_are_separate_queries(self) -> None:
        """browse-edgar will not return both from one type parameter."""
        dg = next(a for a in build_edgar_adapters() if a.name == "edgar_13dg")
        assert dg.forms == ("SC 13D", "SC 13G")  # type: ignore[attr-defined]

    def test_filings_are_not_gated_to_market_hours(self) -> None:
        """EDGAR accepts until roughly 22:00 ET and the heaviest 8-K window is
        just after the close."""
        assert all(not a.market_hours_only for a in build_edgar_adapters())

    @pytest.mark.parametrize("name", ["edgar_8k", "edgar_form4", "edgar_13dg"])
    def test_every_adapter_has_a_normalizer(self, name: str) -> None:
        from signals.adapters.edgar_index import NORMALIZERS

        assert name in NORMALIZERS


class TestFormTypeFiltering:
    """browse-edgar's `type` is a prefix match.

    Asking for type=4 returns 424B2 prospectus supplements, 424B3 and 485APOS
    alongside real Form 4s -- in a live sample only 38 of 100 entries were
    genuinely Form 4. Without filtering, the feed fills with prospectuses stored
    as insider transactions, and the flag budget goes with it.
    """

    def test_a_prefix_match_is_rejected(self) -> None:
        adapter = EdgarIndexAdapter("edgar_form4", ["4"], accepts=["4"])
        group = _group_with_title("424B2 - Morgan Stanley Finance LLC (0001666268) (Filer)")
        assert adapter._wanted(group) is False

    def test_the_requested_form_is_kept(self) -> None:
        adapter = EdgarIndexAdapter("edgar_form4", ["4"], accepts=["4"])
        group = _group_with_title("4 - Chime Financial, Inc. (0001795586) (Issuer)")
        assert adapter._wanted(group) is True

    def test_an_amendment_of_the_wanted_form_is_kept(self) -> None:
        """8-K/A is still an 8-K; base_form is what is compared."""
        adapter = EdgarIndexAdapter("edgar_8k", ["8-K"], accepts=["8-K"])
        group = _group_with_title("8-K/A - Some Corp (0001234567) (Filer)")
        assert adapter._wanted(group) is True

    def test_an_unparseable_title_is_rejected(self) -> None:
        adapter = EdgarIndexAdapter("edgar_form4", ["4"], accepts=["4"])
        assert adapter._wanted(_group_with_title("garbage")) is False

    async def test_a_polluted_feed_yields_only_the_wanted_form(self) -> None:
        body = _atom_with(
            [
                "4 - Real Insider Co (0000000001) (Issuer)",
                "424B2 - Morgan Stanley Finance LLC (0001666268) (Filer)",
                "424B3 - Some Bank (0000000003) (Filer)",
                "485APOS - A Fund (0000000004) (Filer)",
            ]
        )
        adapter = EdgarIndexAdapter("edgar_form4", ["4"], accepts=["4"])
        raws = await adapter.fetch(ctx_for(body))
        assert len(raws) == 1

    def test_the_default_accepts_what_was_asked_for(self) -> None:
        """The safe reading of a prefix-matching API: keep only what you named."""
        adapter = EdgarIndexAdapter("edgar_form4", ["4"])
        assert adapter.accepts == frozenset({"4"})

    def test_every_built_adapter_declares_its_forms(self) -> None:
        for adapter in build_edgar_adapters():
            assert adapter.accepts  # type: ignore[attr-defined]


def _atom_with(titles: list[str]) -> bytes:
    entries = "".join(
        f"""<entry><title>{t}</title>
        <link rel="alternate" href="https://www.sec.gov/Archives/x{i}-index.htm"/>
        <summary type="html">AccNo: 0000000000-26-00000{i}</summary>
        <updated>2026-09-15T21:57:1{i}-04:00</updated>
        <id>urn:tag:sec.gov,2008:accession-number=0000000000-26-00000{i}</id>
        </entry>"""
        for i, t in enumerate(titles)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    ).encode()


def _group_with_title(title: str):
    return group_by_accession(parse_atom(_atom_with([title])))[0]
