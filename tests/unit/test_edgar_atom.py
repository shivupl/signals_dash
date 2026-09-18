"""Atom feed parsing and accession grouping, against real captured feeds."""

from __future__ import annotations

from datetime import timedelta

import pytest

from signals.parsers.edgar_atom import group_by_accession, parse_atom
from signals.parsers.edgar_titles import Role
from tests.conftest import read_fixture

FORM4 = read_fixture("edgar", "atom_form4_current.xml")
EIGHTK = read_fixture("edgar", "atom_8k_current.xml")
EMPTY = read_fixture("edgar", "atom_empty_feed.xml")


class TestParseAtom:
    def test_parses_a_full_window(self) -> None:
        entries = parse_atom(FORM4)
        assert len(entries) == 100

    def test_every_entry_has_an_accession(self) -> None:
        assert all(e.accession for e in parse_atom(FORM4))

    def test_accessions_look_like_accessions(self) -> None:
        for entry in parse_atom(FORM4):
            assert entry.accession.count("-") == 2, entry.accession

    def test_updated_is_timezone_aware(self) -> None:
        """This becomes occurred_at. A naive value here corrupts every latency
        number by however many hours the offset happened to be."""
        for entry in parse_atom(FORM4):
            assert entry.updated.tzinfo is not None
            assert entry.updated.utcoffset() is not None

    def test_entries_carry_links_back_to_the_filing(self) -> None:
        entries = parse_atom(FORM4)
        assert all(e.link and e.link.startswith("https://www.sec.gov/") for e in entries)

    def test_empty_feed_is_not_an_error(self) -> None:
        """The ordinary state outside filing hours -- SEC says 'No recent filings'."""
        assert parse_atom(EMPTY) == []

    def test_malformed_xml_raises(self) -> None:
        from lxml.etree import XMLSyntaxError

        with pytest.raises(XMLSyntaxError):
            parse_atom(b"<feed><entry>truncated")


class TestItemExtraction:
    def test_items_come_free_from_the_index(self) -> None:
        """The finding that removes a whole network round trip from 8-K scoring:
        the summary already carries the item numbers."""
        entries = parse_atom(EIGHTK)
        with_items = [e for e in entries if e.items]
        assert with_items, "no 8-K entry carried item numbers"

    def test_item_numbers_are_well_formed(self) -> None:
        for entry in parse_atom(EIGHTK):
            for item in entry.items:
                major, minor = item.split(".")
                assert major.isdigit() and len(minor) == 2, item

    def test_items_are_deduplicated_and_ordered(self) -> None:
        for entry in parse_atom(EIGHTK):
            assert list(entry.items) == list(dict.fromkeys(entry.items))

    def test_form4_entries_carry_no_items(self) -> None:
        assert all(not e.items for e in parse_atom(FORM4))


class TestGrouping:
    def test_collapses_entries_into_filings(self) -> None:
        entries = parse_atom(FORM4)
        groups = group_by_accession(entries)
        assert len(groups) < len(entries), "grouping did nothing"
        assert sum(len(g.entries) for g in groups) == len(entries)

    def test_one_group_per_accession(self) -> None:
        groups = group_by_accession(parse_atom(FORM4))
        assert len({g.accession for g in groups}) == len(groups)

    def test_a_joint_filing_groups_many_parties(self) -> None:
        """The real shape this exists for: one captured accession spans eleven
        entries -- ten reporting owners and a single issuer."""
        groups = group_by_accession(parse_atom(FORM4))
        biggest = max(groups, key=lambda g: len(g.entries))
        assert len(biggest.entries) >= 3
        assert len(biggest.reporting_ciks) >= 2
        assert biggest.issuer_hint is not None

    def test_issuer_hint_is_the_issuer_not_a_reporter(self) -> None:
        for group in group_by_accession(parse_atom(FORM4)):
            if group.issuer_hint is None:
                continue
            assert group.issuer_hint not in group.reporting_ciks

    def test_groups_are_newest_first(self) -> None:
        groups = group_by_accession(parse_atom(FORM4))
        assert groups == sorted(groups, key=lambda g: g.updated, reverse=True)

    def test_some_groups_lack_an_issuer_at_the_window_edge(self) -> None:
        """Truncation at count=100 really does split filings, which is why the
        adapter needs a grace timer rather than assuming the issuer is present."""
        groups = group_by_accession(parse_atom(FORM4))
        assert any(not g.has_issuer for g in groups), (
            "fixture happened to have no split group; the grace path still needs to exist"
        )

    def test_primary_prefers_the_issuer(self) -> None:
        groups = group_by_accession(parse_atom(FORM4))
        with_issuer = [g for g in groups if g.has_issuer]
        assert with_issuer
        for group in with_issuer:
            assert group.primary.role is Role.ISSUER

    def test_group_updated_is_the_latest_of_its_entries(self) -> None:
        for group in group_by_accession(parse_atom(FORM4)):
            assert group.updated == max(e.updated for e in group.entries)

    def test_grouped_updates_are_tightly_clustered(self) -> None:
        """Parties to one filing share an acceptance time, which is why grouping
        inside a single response is reliable rather than merely likely."""
        for group in group_by_accession(parse_atom(FORM4)):
            spread = max(e.updated for e in group.entries) - min(e.updated for e in group.entries)
            assert spread < timedelta(minutes=5), f"{group.accession} spread {spread}"

    def test_empty_feed_groups_to_nothing(self) -> None:
        assert group_by_accession(parse_atom(EMPTY)) == []
