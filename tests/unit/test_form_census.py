"""The rename detector.

Its whole reason for existing: when SEC renamed SC 13G to SCHEDULE 13G, the 13D/G
query returned nothing, so the new spelling never appeared anywhere the system was
looking. This looks at the unfiltered firehose instead, where it was plainly
visible the whole time.
"""

from __future__ import annotations

import httpx

from signals.adapters.base import FetchContext
from signals.adapters.form_census import (
    FormCensusAdapter,
    family,
    unclaimed_spellings,
)
from signals.http import SourceClient
from tests.fakes import FakeClock

UA = "Signals/0.1 (test@example.com)"
CLAIMED = ("8-K", "4", "SC 13D", "SC 13G", "144")


class TestFamily:
    def test_both_schedule_spellings_are_one_family(self) -> None:
        assert family("SC 13G") == family("SCHEDULE 13G") == "13G"

    def test_amendments_collapse_into_the_base(self) -> None:
        assert family("SCHEDULE 13D/A") == "13D"
        assert family("4/A") == "4"

    def test_an_ordinary_form_is_its_own_family(self) -> None:
        assert family("8-K") == "8-K"
        assert family("144") == "144"


class TestUnclaimedSpellings:
    def test_the_rename_that_cost_nine_months_is_caught(self) -> None:
        got = unclaimed_spellings(["SCHEDULE 13G", "4", "8-K"], CLAIMED)
        assert got == {"13G": {"SCHEDULE 13G"}}

    def test_an_amendment_of_a_renamed_form_reports_the_base(self) -> None:
        assert unclaimed_spellings(["SCHEDULE 13D/A"], CLAIMED) == {"13D": {"SCHEDULE 13D"}}

    def test_the_normal_state_is_silence(self) -> None:
        assert unclaimed_spellings(["4", "4/A", "8-K", "SC 13G", "144"], CLAIMED) == {}

    def test_forms_from_families_we_do_not_want_are_ignored(self) -> None:
        """The census sees the whole firehose. Complaining about S-1s and N-PORTs
        would bury the one line that matters."""
        assert unclaimed_spellings(["S-1", "N-PORT", "X-17A-5", "10-K", "3"], CLAIMED) == {}

    def test_a_claimed_amendment_is_not_a_surprise(self) -> None:
        assert unclaimed_spellings(["SC 13D/A"], CLAIMED) == {}

    def test_several_renames_at_once(self) -> None:
        got = unclaimed_spellings(["SCHEDULE 13G", "SCHEDULE 13D"], CLAIMED)
        assert got == {"13G": {"SCHEDULE 13G"}, "13D": {"SCHEDULE 13D"}}

    def test_once_both_spellings_are_claimed_it_goes_quiet(self) -> None:
        """What the fix looks like from here."""
        claimed = (*CLAIMED, "SCHEDULE 13D", "SCHEDULE 13G")
        assert unclaimed_spellings(["SCHEDULE 13G", "SCHEDULE 13D/A"], claimed) == {}


def feed(forms: list[str]) -> bytes:
    entries = "".join(
        f"""<entry><title>{form} - Some Filer (0000000001) (Filer)</title>
        <link rel="alternate" href="https://www.sec.gov/Archives/edgar/data/1/x-index.htm"/>
        <summary type="html">AccNo: 0000000000-26-00000{n}</summary>
        <updated>2026-09-28T12:00:00-04:00</updated>
        <id>urn:tag:sec.gov,2008:accession-number=0000000000-26-00000{n}</id></entry>"""
        for n, form in enumerate(forms)
    )
    return (
        f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    ).encode()


def rig(forms: list[str]):
    body = feed(forms)
    clock = FakeClock()
    ctx = FetchContext(
        http=SourceClient(
            UA,
            clock=clock,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
        ),
        clock=clock,
        state={},
    )
    return FormCensusAdapter(CLAIMED), ctx


class TestAdapter:
    async def test_a_renamed_form_raises_a_system_event(self) -> None:
        adapter, ctx = rig(["4", "8-K", "SCHEDULE 13G"])
        raws = await adapter.fetch(ctx)
        assert len(raws) == 1
        event = adapter.normalize(raws[0])[0]
        assert event.source == "system"
        assert event.event_type == "form_unclaimed"
        assert event.payload["spelling"] == "SCHEDULE 13G"
        assert event.payload["severity"] == 80
        assert "SCHEDULE 13G" in event.payload["headline"]

    async def test_an_ordinary_feed_says_nothing(self) -> None:
        adapter, ctx = rig(["4", "4/A", "8-K", "144", "S-1", "10-Q"])
        assert await adapter.fetch(ctx) == []

    async def test_it_reports_each_surprise_once(self) -> None:
        """Every fifteen minutes forever would be worse than not noticing."""
        adapter, ctx = rig(["SCHEDULE 13G"])
        assert len(await adapter.fetch(ctx)) == 1
        assert await adapter.fetch(ctx) == []

    async def test_the_event_belongs_to_no_company(self) -> None:
        adapter, ctx = rig(["SCHEDULE 13D"])
        event = adapter.normalize((await adapter.fetch(ctx))[0])[0]
        assert event.payload["market_wide"] is True
