# S&P 500 as a second monitor

## Problem

Signals watches 40 hand-picked companies. Everything else in the market is
invisible, including the 500 largest listed companies in the US. The ask: add an
"S&P 500" monitor you can click into and see flags for every index member,
without turning the existing 40-name feed into something you stop reading.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| What the index view shows | Everything is ingested; the view defaults to score >= 50 | Index-wide coverage that stays scannable. Your 40 keep threshold 30. |
| Prices for index names | On demand | A 540-ticker refresh every 15 minutes would be the heaviest thing in the system, for charts nobody opened. |
| Membership list | Committed snapshot, refreshed by hand | Index changes land as a reviewable commit instead of shifting silently at runtime. |
| Earnings 8-Ks in the index view | Category unchecked by default | ~25 flags/day for three weeks a quarter, all of them "company reported earnings". One click restores them. |

## Measured volume

Every one of the 500 index CIKs, over 2026-09-18 to 2026-09-25, via
`data.sec.gov/submissions`:

| Form | Per day | Total |
|---|---|---|
| Form 4 | 38-75 (avg 54) | 323 |
| 8-K | 10-22 (avg 16) | 94 |
| SC 13D/G | 0 | 0 |

So roughly 70 extra events a day. For comparison the existing 40 names produce
3-27. This is the number that made the design small: no new rate-limit machinery
is needed, and 13D/G volume for the index is nil.

## Architecture

### Membership

```sql
create table universe_member (
  company_id bigint not null references company(id) on delete cascade,
  universe   text   not null,          -- 'core' | 'sp500'
  primary key (company_id, universe)
);
create index universe_member_universe_idx on universe_member (universe, company_id);
```

`company.watched` keeps its current meaning -- "ingest this company" -- and is
derived at seed time as "member of at least one universe". **The worker pipeline
therefore does not change.** `WATCHED_CIKS`, resolution, scoring, dedupe and
promotion are all untouched; a universe is a lens over stored events, not a
second ingest path.

Membership is keyed by **CIK, not ticker**. The list's 503 tickers are 500
companies: GOOGL/GOOG, FOX/FOXA and NWS/NWSA are dual share classes of one
filer, and matching by ticker would invent rows that no filing can ever resolve
to.

16 of the existing 40 are index members, so the sets overlap. The union is 527
companies.

### The snapshot

`config/sp500.yml`, committed:

```yaml
captured: 2026-09-25
source: <constituents csv url>
members:
  - {ticker: MMM, cik: "0000066740"}
```

`scripts/refresh_sp500.py --allow-network` refreshes it and prints an
added/removed diff. The `--allow-network` opt-in matches
`capture_fixtures.py`: nothing in this repo reaches the network by accident.

Seeding reconciles rather than appends -- a company no longer in the file loses
its `sp500` membership, and loses `watched` unless it is also in `core`. The
worker logs a warning when `captured` is more than 90 days old, and
`/api/meta` reports the date so staleness is visible rather than assumed.

### Reconciliation sweep

The one pipeline change. `EdgarBackfillAdapter` currently walks every watched
company every 30 minutes; at 527 companies that is a ~500-request burst. It
becomes:

- `core` members every sweep (40 requests, unchanged behaviour for your 40)
- `sp500`-only members in four rotating slices, so the index is fully covered
  every two hours (~120 requests per sweep)

That is ~0.3 req/s against the 8 req/s budget, in the existing LOW priority
lane. The 3-day lookback leaves a large margin over a 2-hour rotation, so a
slice cannot miss a filing.

### Prices

Unchanged for `core`: 15-minute closes and daily earnings dates. For an index
name:

- `price_at` is stamped per event by the existing quote path -- no change
- the 90-day chart is fetched the first time that company page is opened,
  time-boxed to ~4s, then cached in `price_daily` like any other row
- on timeout the page renders without the chart and the next open fills it

The API gains a `PriceService`, which it does not have today. This is allowed by
the layering contracts (they forbid `parsers` from importing IO and `scoring`
from importing store/bus/adapters/pipeline; neither covers `api` -> `prices`).

### API

- `GET /api/feed?universe=core|sp500|all`, default `core`, joined through
  `universe_member`. Composes with every existing filter.
- `GET /api/active?universe=sp500&limit=10` -- most active names by flag count
  in the current window. A separate endpoint rather than a mode flag on
  `/api/watchlist`, so each has one meaning and its own tests. The rail cannot
  show 500 rows, and these names have no refreshed price data to sort by.
- `GET /api/company/{ticker}` -- adds on-demand price fill and `in_universes`.
- `GET /api/meta` -- adds `sp500_captured`.

### UI

- A segmented control in the header: `My 40 | S&P 500`. The universe lives in
  the URL with the other filters, so a link keeps working.
- Switching to S&P sets the score slider to 50 and unchecks the Earnings
  category. Both are display state written to the URL; the server applies no
  per-universe defaults, so the URL always describes what is on screen.
- The rail shows the 40 in the core view and "Most active" in the S&P view.
- Header: "N flags - M routine - 500 in S&P 500".
- A company page carries an `S&P 500` badge for members.
- The ticker typeahead searches all 527 ingested names rather than the 40.

## Testing

- **Seeding**: membership add and remove, `watched` recomputation, the
  dual-class CIK collapse, and the stale-snapshot warning.
- **Refresh script**: a small committed CSV fixture, parsed offline. Covers the
  quoted-comma trap -- the source CSV has commas inside quoted fields, so naive
  column splitting reads the wrong column (this bit during measurement).
- **Store**: the universe filter on `feed`, and the active-companies query.
- **API**: the new params, and the on-demand price fill against a fake provider.
- **UI**: the universe survives a URL round trip, and switching sets the
  slider and the category default.

All existing gates stay green: ruff, `mypy --strict`, both import-linter
contracts, `tsc --noEmit`, and the full pytest suite before every commit.

## Risks

- **Earnings season.** ~500 earnings 8-Ks a quarter at score 60, concentrated in
  three weeks. Mitigated by the default above; revisit after one season.
- **Snapshot staleness.** Mitigated by the 90-day warning and the meta field.
  Roughly 20 index changes a year, so a quarterly refresh suffices.
- **Unresolved growth.** More entities pass the watched filter, so `unresolved`
  grows faster. The existing `(source, external_id)` dedupe bounds it.
- **A quiet S&P feed would be a bug, not calm.** The index files every day, so
  after deploy the S&P view showing nothing means something is broken. Verify
  against a same-day submissions count, not against expectations.

## Out of scope

Per-universe thresholds in the database, sector and index-weight filters, and
any UI for creating universes -- the config file is that interface. Form 144 as
a new source stays a Phase 2 item.
