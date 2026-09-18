"""Attach an event to a company.

Phase 1 is the easy case -- EDGAR and the halt feed both carry hard identifiers.
The indirection through ``company_alias`` is built now anyway, because WARN
notices and court dockets in Phase 2 name subsidiaries and holding companies,
and retrofitting it later would mean rewriting every adapter.

Events that fail to resolve are still stored. The unresolved table is a work
list, not a bin.
"""

from __future__ import annotations

from ..models import Company, NormalizedEvent
from ..store.base import Store


class Resolver:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def resolve(self, event: NormalizedEvent) -> Company | None:
        """Strongest identifier first: CIK, then ticker, then legal name."""
        if not event.company_key:
            await self._record(event)
            return None

        company = await self._store.find_company(event.company_key)
        if company is None:
            await self._record(event)
        return company

    async def _record(self, event: NormalizedEvent) -> None:
        # The queue exists to be worked through, so the name has to be something
        # a person can act on. Falling back to "?" produced a list of question
        # marks -- technically a record of failure, useless as a work list.
        raw_name = (
            event.company_key.name
            or event.company_key.ticker
            or event.company_key.cik
            or event.summary
            or event.external_id
        )
        # Keyed on the filing so re-polling does not append a row each time.
        await self._store.record_unresolved(
            event.source, event.external_id, raw_name, dict(event.payload)
        )
