"""Re-read Form 4 documents for rows stored before a payload field existed.

Dedupe keys on the accession, so the normal pipeline will never revisit a filing
it already holds. When the payload gains a field -- sale values, for the insider
table -- this fetches the document again and merges just the new facts in.
"""

from __future__ import annotations

import logging

from .clock import SystemClock
from .config import Settings
from .http import SourceClient
from .parsers.form4_xml import Form4ParseError, parse_form4
from .ratelimit import Priority
from .store.pg import PgStore

log = logging.getLogger(__name__)


async def rehydrate_form4(settings: Settings) -> dict[str, int]:
    store = await PgStore.connect(settings.database_url)
    http = SourceClient(settings.sec_user_agent, clock=SystemClock())
    stats = {"rows": 0, "updated": 0, "failed": 0}
    try:
        for row in await store.form4_missing_sales():
            stats["rows"] += 1
            url = f"{row['url'].rsplit('/', 1)[0]}/{row['external_id']}.txt"
            try:
                doc = parse_form4(await http.get_bytes(url, priority=Priority.LOW))
            except (Form4ParseError, Exception) as exc:  # noqa: BLE001
                log.warning("rehydrate %s failed: %s", row["external_id"], exc)
                stats["failed"] += 1
                continue
            await store.update_payload(
                "edgar_form4",
                row["external_id"],
                {"sale_shares": doc.sale_shares, "sale_value": doc.sale_value},
            )
            stats["updated"] += 1
    finally:
        await http.aclose()
        await store.close()
    return stats
