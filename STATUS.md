# Signals — build status

Build log for Phase 1: what was built, what was measured live, and where the
original design turned out to be wrong.

Last updated: 2026-09-18 03:40 ET (Phase 1 complete, running)

| # | Step | State | Notes |
|---|------|-------|-------|
| 0 | Scaffold: compose, Dockerfile, Makefile, ruff/mypy/import-linter | **done** | All gates green |
| 1 | Schema, migrations, watchlist seed | **done** | 8,022 companies, 26,466 aliases, 40 watched |
| 2 | `clock`, `models`, `config`, `http`, `ratelimit` | **done** | Token bucket ≤8 req/s, breaker, NYSE calendar |
| 3 | EDGAR index adapter + runner | **done** | Live. Dedupe exact |
| 4 | Form 4 hydration, Reporting/Issuer rule | **done** | 30 real documents, hint_mismatch 0 |
| 5 | Form 4 scoring + cluster promotion | **done** | Live: grants score 0, real buys surface |
| 6 | 8-K item routing + classifier stub | **done** | Items parsed off the index, zero extra fetches |
| 7 | FastAPI + React feed | **done** | |
| 8 | Redis bus + `WS /ws` | **done** | Flag reaches the browser ~90 ms after it is stored |
| 9 | Halts adapter + resume handling | **done** | Nasdaq Trader RSS; verified against the live feed |
| 10 | Prices, `price_at`, earnings dates | **done** | 400 closes, 39 earnings dates, best-effort stamping |
| 11 | Watchlist rail | **done** | Weekly change, earnings marker, click to filter |
| 12 | `/stats` + staleness watchdog | **done** | Stale sources appear in the feed itself |

**Phase 1 is feature-complete.** 469 tests; `ruff`, `mypy --strict`, `tsc` and both
import-linter purity contracts clean.

## What is actually feeding the UI

**Live data.** `docker compose up -d` runs a worker with four adapters -- `edgar_8k`,
`edgar_form4`, `edgar_13dg`, `halts` -- plus a price loop and a watchdog. It polls
06:00-22:00 ET on trading days and idles otherwise. Only the 40 watched companies
are stored, so the feed is quiet by design: most days, most of them do nothing.

`make replay` still exists for demo data, and `python -m signals worker --all`
stores every filer -- that is a diagnostic for checking ingest at volume, not how
to run it. If you use either, delete the rows afterwards
(`delete from event e using company c where c.id = e.company_id and not c.watched`)
or the feed and the rail will disagree.

## Measured, live (2026-09-16, 16:52-17:05 ET)

Worker polling SEC for ~13 minutes during the post-close 8-K peak:

- **Dedupe exact.** 109 8-K rows / 109 distinct ids, 69 Form 4 / 69 distinct.
  Re-polling the same window hundreds of times inserted nothing twice.
- **Request rate 0.56 req/s**, against a self-imposed 8/s and SEC's 10/s.
- **Detection latency** for filings accepted after startup: 8-K p50 29.5s /
  p95 46.3s; Form 4 p50 34.8s / p95 73.5s. Fastest observed 13.8s.
- **Form 4 resolution 100%**, 8-K 91%. The unresolved 9% were checked rather
  than assumed: they are entities with no ticker -- `SIMON PROPERTY GROUP L P`
  (the operating partnership, not the listed REIT), non-traded REITs, `IPALCO`
  (a utility subsidiary that files because of public debt), a non-traded BDC.
  Correct behaviour, not a failure.
- A real `www.sec.gov: timeout` occurred and the runner logged it, backed off and
  recovered without stopping the other adapters.

### The p50 target needs revisiting

The plan targets p50 under 15s. Measured is roughly double that. Our poll
interval can account for at most 2s of it, so the remainder is SEC's own delay
between accepting a filing and publishing it to `getcurrent`. Polling faster
would not help. Either the target moves to ~30s, or it needs a different
endpoint -- and the only real-time SEC feed is the paid Public Dissemination
Service.

## Two defects found and fixed after the first live run

**`type` is a prefix match.** `browse-edgar?type=4` returns 424B2 prospectus
supplements, 424B3 and 485APOS alongside Form 4s -- in a live sample only 38 of
100 entries were genuinely Form 4. Adapters now declare the base forms they
accept and discard the rest before storing. Form 4 resolution went from 42% to
100%. `8-K` behaves the same way but benignly: it also returns `8-K/A`, which is
wanted, so amendments match on base form.

**The unresolved queue was a growth log.** `record_unresolved` fired on every
poll, so 16 distinct entities became 6,668 rows in four hours -- about 37,000 a
day. It is now keyed on the filing's accession with `on conflict do nothing`,
the same dedupe contract as `event` (migration `003`). Its `raw_name` also fell
back to `"?"`, making the list unactionable; it now falls back to the event
summary.

## Step 4, verified live (2026-09-17, 04:15 ET)

Run outside the ingest window against the overnight index, so latency figures
would be meaningless -- the counters are the point:

- **30 documents hydrated, `hint_mismatch` 0**, no parse or fetch failures.
  A non-zero mismatch would mean the title parser disagrees with the document
  EDGAR validated.
- **15 filings skipped without spending a request** -- the hydration budget:
  an unwatched company's insider is not worth a fetch.
- **1 accession held rather than guessed**, waiting for its issuer entry.
- Rounds 2 and 3 fetched nothing: the same filing stays in the window for many
  polls and must not be re-fetched on each.

### Two things the spec got wrong

**The XML document has no predictable name.** The spec says "clean XML at a
predictable path". Live filings use `ownership.xml`, `rdgdoc.xml` and
`wk-form4_1789609812.xml`. Fetching `{accession}.txt` -- the full submission,
5-25KB, with the XML inline -- is one request instead of a directory listing
plus a document.

**The 10b5-1 flag has three encodings.** A live sample of 17 filings carried
`1`, `0` and `false` in the same field. A plain truthiness test reads `"false"`
as yes and applies the -30 penalty to filings that explicitly said no.

## Step 5, verified live (2026-09-17, 16:31-16:43 ET, just after the close)

Watchlist temporarily widened to 4,000 companies so insider buys would appear in
a short window, then restored to 40.

- **49 grants, sales and option exercises scored 0.** Codes A, M, F and S are
  compensation mechanics; flagging them would mean flagging every vesting event
  in the market. This is most of what Form 4 traffic actually is.
- **2 genuine open-market purchases surfaced**, and the pair is a good
  illustration of the scoring:

  | Ticker | Score | Parts | What it was |
  |---|---|---|---|
  | CORZ | 45 | base 40 + director 5 | A director bought $97,980 of their own company |
  | CRBG | 20 | base 40 + large_dollar 10 - plan 30 | Nippon Life bought $7.4M -- but on a 10b5-1 plan |

  The second one is the whole argument for the 10b5-1 penalty. A $7.4M purchase
  looks enormous; scheduled months in advance, it says nothing about what the
  buyer thinks today. At 20 it stays below the threshold and does not flag.
  Without the penalty it would score 50 and read as a high-priority signal.

- **Latency, live-captured only**: 8-K p50 45.0s, Form 4 p50 46.9s, with 80
  backfilled events correctly excluded.

### A measurement trap worth remembering

The first attempt reported an 8-K p50 of 985s and a p95 of 7.5 hours. The cause
was the procedure, not the code: truncating `event` while the previous worker
was still polling let it re-insert its whole window, with `live_capture` computed
against a start time twelve hours old. Stop the worker before clearing the table.

### One thing not yet observed live

**No cluster promotion has fired.** It needs three distinct insiders buying the
same company inside thirty days, which did not happen in a twelve-minute window.
The logic is covered by 13 Postgres-backed tests, including the case that matters
-- three 10b5-1 buys at 10 each, promoted to 35, crossing the flag threshold.

## Steps 8-12, built overnight 2026-09-18

**End-to-end selftest**, through every layer with a synthetic halt: processor ->
Postgres -> Redis -> hub -> websocket client. `event.new` arrived 89 ms after
`process()`; `event.updated` followed with a live `price_at` of 67.82.

### More places the spec was wrong

**`M` is not a market-wide circuit breaker.** In live data it is a per-stock
five-minute volatility pause on NYSE / Arca / AMEX listings. Only `MWC1-3` are
market-wide. Treating `M` as market-wide would have stored ordinary pauses with no
company and shown them to everyone.

**The reason-code table was incomplete.** `T3` and `H11` both appear in live feeds
and were not in it. Added, along with H4, H9, T6, T8.

**Repeat volatility pauses would blow the flag budget.** One symbol in a captured
feed was paused 31 times in one day. At LUDP = 30 that single ticker exhausts a
20-flag day. The first pause of a day scores 30; repeats score 15 and stay on the
timeline unflagged. News halts are never dampened.

### Deliberate deviations

**The watchdog measures ingest minutes, not market minutes.** The spec says 30
market-minutes. But session time stops at 16:00, which is exactly when the
heaviest 8-K window starts -- a source dying at 16:05 would go unnoticed until
10:00 the next day.

**The websocket sends no snapshot.** The client refetches over HTTP on every
(re)connect and applies pushed messages on top. Same guarantee, one code path,
and HTTP stays the single source of truth.

### Not yet observed live

- **Cluster promotion** (needs 3 distinct insiders in 30 days) -- 13 Postgres tests.
- **A watched company being halted** -- live feed parsed and scored correctly, but
  none of the 40 was halted overnight. 43 tests, including quotes-before-trading.
- **A watchdog alarm** -- 11 tests on a fake clock.

## Reliability and latency, measured 2026-09-18 evening

**Failures fell from ~68/hour to ~9/hour** after hedged index requests, recognising
the halt feed's CDN challenge page, and free first-failure retries. Zero timeouts
in two hours; the remainder is SEC recycling connections, which recovers on the
next poll. The halt feed had 0 failures in 118 polls at a 30 s interval.

**Latency is bimodal.** The fast path is 21-33 s from acceptance to stored (four
NVDA Form 4s). An independent probe put the floor at about 30 s: that is how long
SEC takes to publish to `getcurrent`. But SEC's index requests are slow 30% of
the time (p50 1.3 s, p90 12 s, max 39 s), so the configured 2 s poll is really
6-8 s, and a realistic p50 is 45-60 s.

**The slow tail was the host machine sleeping.** Five of nine live captures took
150-1,400 s. Two hypotheses were tested and ruled out first (a connection pinned
to a stale cache node; the index itself lagging). The `poll_gap_s` forensics then
showed polling looked healthy during a 23-minute delay -- which pointed away from
SEC and at the host. `pmset -g log` settled it: the laptop sleeps after one idle
minute and went through 269 sleep cycles in a day and a half, dozens during market
hours. Every slow capture lines up with a sleep window to the second (woke
16:23:13, stored 16:23:18); every fast capture happened while it was awake. During
a five-second dark wake the worker finds its connections dead, reconnects, reads
the index, and is suspended again -- which is also why those episodes always ended
with "Server disconnected", and why `poll_gap_s` read small: a suspended
container's monotonic clock does not advance.

So: awake, latency is 21-33 s. Asleep, the system is blind. This is a laptop
problem, not a pipeline problem, and the fix is where it runs (see Open decisions).
The watchdog did fire correctly on the longer outages -- its first live alarms.

The reconciliation sweep bounds the damage regardless: nothing is lost, only late.

## Phase 1.5: the dashboard (2026-09-21)

No new sources; backend changes only where a view needed them.

- **System events left the feed.** They were 9 of 11 rows. They now live in a
  status strip; `/api/feed` excludes them unless asked. The watchdog keeps **one
  row per outage**, updated in place while its duration counts up, plus a single
  recovered event. A wall-clock jump the process clock did not see is recorded
  once as *the host was suspended* rather than blamed on every source -- which is
  what those nine alarms actually were.
- **Header and rail agree by construction.** `X-Flag-Count` comes from the same
  predicate as the rows, and the rail takes the same threshold, source and
  event-type filters. A row scoring 0 is "routine", never a flag.
- **Filter bar, all state in the URL.** Multi-select tickers, sources and event
  types; a score slider; today / 7d / 30d / custom, where "today" is the market's.
- **A normalized `category`** on every event, mapped in one module. `distress`
  (restatement, bankruptcy, delisting, auditor change) was added to the requested
  list: those are the highest-scoring events and did not belong under "other".
- **Company page** with a price timeline, scoped events, an insider table grouped
  by CIK, and unresolved filings whose filer name starts like the company's.
- **The flag threshold is a runtime setting**, re-read by the worker every 30 s.
  Guarded by `ADMIN_TOKEN`; read-only without one. Separate from the display
  slider, which is per-viewer and unsaved.

Two deliberate departures from the brief. The display threshold is sent to the
server rather than applied client-side: the feed has a row limit, and filtering
after a limit silently drops matches. And there was no existing password to put
the settings panel behind, so `ADMIN_TOKEN` was added.

## Open decisions

0. **Where this runs.** On a laptop that sleeps, "five trading days unattended" is
   not achievable. Either keep the machine awake during the ingest window
   (`make awake`; does not survive a closed lid) or move to the small VPS the
   design always assumed.

1. **The p50 < 15 s target.** Measured p50 is 30-45 s and the poll interval
   accounts for at most 2 s of it; the rest is SEC's own publication lag. The
   target should probably move to ~45 s.
2. **The flag threshold.** 7.01 / 8.01 8-Ks score 20, below the threshold, so the
   routine-press-release flood the design worried about does not occur. Revisit
   after a week of real data.

## What is left

Phase 1's own "definition of done" is about time, not code: five trading days
unattended, under 20 flags a day, and two weeks of actually reading it. Then
Phase 2 (WARN, CourtListener, borrow rates, short interest, company timeline,
outcome log) -- for which `price_at` is now being recorded from day one.

## Commands

    make up        # postgres, redis, worker, api  ->  http://localhost:8000
    make logs      # tail worker + api
    make test      # 469 tests
    make web       # rebuild the UI into web/dist
    make psql      # poke at the data
    make seed      # re-read config/watchlist.yml (then: docker compose restart worker)
    make fixtures  # re-capture test fixtures from the live network
