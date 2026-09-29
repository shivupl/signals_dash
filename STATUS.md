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

## Deployed (2026-09-22)

Runs on a 1 vCPU / 2 GB DigitalOcean droplet at https://signals.shivuppal.com
from `docker-compose.prod.yml`; see `deploy/README.md`. Caddy terminates TLS
(Let's Encrypt, auto-renewing), and only 80/443 are published. The local
database was carried over with `pg_dump`/`pg_restore` (79 events, 8,022
companies, 3,640 price rows; counts matched after restore). A reboot test
brought all five services back unattended. The laptop stack is stopped so SEC
is polled from one place only.

## S&P 500 universe (2026-09-25)

A second monitor: `My 40` (threshold 30) and `S&P 500` (threshold 50, earnings
hidden by default), switched in the header and carried in the URL.

**Measured before designing.** All 500 index CIKs over six days via
`data.sec.gov/submissions`: 38-75 Form 4s a day (323 total), 10-22 8-Ks (94), and
zero 13D/Gs. About seventy events a day, not the 200-500 I guessed -- which is why
this needed no new rate-limit machinery.

**Membership is a lens, not a second pipeline.** `universe_member` names which
companies are in which monitor; `company.watched` keeps meaning "ingest this
company" and is derived from membership at seed time. The worker still reads one
flat set of CIKs, so resolution, scoring, dedupe and promotion are untouched.

**Matched on CIK, not ticker.** The index's 503 symbols are 500 filers -- GOOGL/GOOG,
FOX/FOXA, NWS/NWSA are dual classes of one filer. A ticker match would have
created rows no filing could ever resolve to. 16 of the 40 are index members, so
the union is 524 companies.

**Two loads that had to be contained.** The reconciliation sweep visited every
watched company every 30 minutes; at 524 that is a 500-request burst, so core is
now swept every time and the index in rotating quarters (full coverage every two
hours, ~0.3 req/s against an 8 req/s budget). And the price loop silently widened
with the universe -- `watched_companies()` returned all 524 -- which would have
meant a 524-ticker pull every fifteen minutes; it is now scoped to core, with
index charts fetched on first open and cached.

**Caught in review, not by tests:** the filter pill read "Earnings" while earnings
were being *hidden*. It now reads "no earnings".

**First live signals:** a Berkshire Hathaway open-market buy in Lennar (70) and a
delisting notice for PSKY (85) -- both index names the old 40-name watchlist could
never have surfaced.

## Form 144 (2026-09-26)

Advance notice of insider selling: a 144 is filed *before* the sale, where a Form
4 reports it afterwards. New source `edgar_144`, new category "Planned sale (144)"
kept apart from `insider_sell` so intent and receipt can be read separately.

**Measured first.** 3-10 notices a day for the 40, 8-21 for the index, so about
eighteen a day combined -- enough that the calibration decides whether this is
signal or noise. Base 20 keeps a routine scheduled sale under both thresholds;
+15 over $10M, +10 over 1% of shares outstanding, +10 for an officer/director/10%
holder, -10 for a 10b5-1 plan, +25 for three sellers inside 30 days. The ceiling
is 80, below a delisting (85) -- an intention should not outrank an event.

**Two traps found in live data, both silent:**

- The XML's **namespace prefix belongs to the filing agent**, not the form: one
  day's filings carried `ns2:`, `own:` and no prefix at all. A parser that
  hard-codes one reads nothing and reports it as an empty notice. Namespaces are
  stripped before any lookup, and the three styles are each a fixture.
- **"Former Officer" contains "Officer".** Relationships seen include `Officer`,
  `10% Stockholder`, `Affiliate`, `Former Officer` and `Member of immediate family
  of any of the foregoing`. A former officer or a relative selling says much less,
  so those are excluded explicitly -- the same shape of bug as matching "Vice
  President" when looking for a president.

Also: `planAdoptionDates` comes absent, present-but-empty, or dated. Only a dated
plan is a plan; treating the element's presence as proof would quietly take 10
points off every notice that has an empty one.

**The company is the `(Subject)` entry**, not `(Issuer)` -- `Role.SUBJECT` already
existed for 13D/G. A `(Reporting)` CIK belongs to the person selling, who will
never be on a watchlist, so resolving from it would drop every notice.

**Asymmetry with Form 4, on purpose:** there is no retroactive promotion for
notices. A 144 already scores on its own merits, so the cluster count includes the
notice being scored and earlier notices keep the scores they were given.
Generalising the promoter to a second source is more than this earns.

Verified locally by driving the real adapter over the captured fixtures: Rubrik's
CEO noticing $56.9M scored 45, Cerebras' $24.8M scored 35 because it is plan-based,
and a small plan sale and a family trust both scored 20.

## Backfill and CI (2026-09-28)

**CI.** Five gates on every push in GitHub Actions: ruff, `mypy --strict`, the
import-linter contracts, the full pytest suite against a Postgres service, and
`tsc` through the web build. Both jobs were rehearsed in throwaway containers
first -- fresh dependency resolution, no `.env`, no warm volumes -- because the
point is to catch what only a clean machine sees. That rehearsal is also how the
newer mypy and pytest majors got checked before the first run.

Two tests had to be rewritten to live in CI. Both asserted wall-clock budgets: one
expected `process()` to return inside 250ms against a provider sleeping 300ms, and
on a loaded laptop the same correct code took 3.3 seconds. They now assert
ordering -- the flag is published and the price is still NULL -- so a slow machine
makes them *more* reliable rather than flaky. A flaky suite would have destroyed
the value of the signal on day one.

**Backfill.** `python -m signals backfill` loads months of filing history so the
scoring constants can be measured instead of argued about. It drives the live
reconciliation adapter rather than reimplementing the walk, so a backfilled event
is indistinguishable from one caught live and dedupes against it. Older
submission shards are fetched only when `filings.recent` (capped at 1,000 filings)
does not reach the window.

**Prices load before filings, and that ordering is load-bearing.** `price_at` for
an event older than half an hour comes from `close_on_or_before` -- the close of its
own market date. Filings first would leave every historical flag with a NULL price,
which is the one number the whole exercise depends on. Verified: all 34 events in
the first trial run carried a price.

**Splits are handled, not documented around.** `price_daily.adj_close` now sits
beside `close`, from the same yfinance call (`auto_adjust=False` returns both).
Raw for "what did it cost", adjusted for "what did it return" -- a return computed
across a 10-for-1 split is wrong by the split ratio.

**Survivorship is a real limitation and is not fixed.** `company` is seeded from
`company_tickers.json`, which lists today's filers with tickers, so anything
delisted or acquired before that snapshot was never in the table and cannot appear
in the corpus. The distinction that matters: this does *not* bias a materiality
study -- whether a 4.02 deserved a flag has nothing to do with the company's later
fate -- but it biases any forward-return study upward, because the companies that
died are missing. Point-in-time index membership is recoverable from the
constituents file's own git history if that ever matters enough to build.

**A bug the backfill found.** Adding 144 to the swept forms never added its
hydration, so the sweep handed a document-less payload to the Form 4 normalizer and
raised `KeyError` -- breaking reconciliation for any watched company that filed a
144. It was live on the server for a day and silent until it happened. Now hydrated
and dispatched properly, with a regression test.

Also: the backfill configures logging. Without it a twenty-minute run printed
nothing and looked hung for eight minutes while working perfectly.

## What the corpus found immediately (2026-09-28)

24 months for the 40 watchlist names: **11,385 events, 2024-09-16 to 2026-09-25,
98.5% carrying a price**. At threshold 30 that is **2,373 flags over 496 market
days = 4.8 flags/day** -- comfortably inside the plan's under-20 budget, and the
first time that number has been measured rather than asserted.

**13D/G had been dead since December 2024.** The report showed every source
running to last week except 13D/G, which stopped on 2024-12-16. SEC renamed the
forms: `SC 13G` became `SCHEDULE 13G`. Two failures stacked:

- `browse-edgar`'s `type` is a prefix match, and `SC 13D` does not prefix
  `SCHEDULE 13D`, so the index poll found nothing under either name;
- `score_13dg` stripped only the `"SC "` prefix, so a `SCHEDULE 13D` would have
  scored **zero** even if it had arrived -- indistinguishable from routine noise.

Activist stakes score 85 and are the highest-value signal in the system. Repairing
it recovered **430 events**, taking 13D/G from 123 to 553 and its flags from 50 to
254. The stored forms show both spellings coexisting in late 2024, which is why
both are now polled rather than just the new one.

Two years of history found this in one query. Nothing in the live system could
have: a source that returns an empty list looks exactly like a quiet week.

**The flag budget is dominated by one score sitting on the threshold.** Of 2,373
flags, **750 are Form 144 notices scoring exactly 30** -- base 20 plus the insider
bonus 10, landing precisely on the bar. That is the trap the original plan named
for 7.01/8.01 ("30 lands exactly on the threshold and will dominate volume"),
repeated by a source added a day earlier. Dropping `FORM144_INSIDER` to 5, or
moving the threshold to 31, takes 4.8 flags/day to 3.3. Which of those is right is
an ablation question, and the corpus can now answer it.

**Calibration evidence worth acting on.** RKLB's CEO noticed a **$465,450,000**
sale (0.86% of the company) which scored **35** -- below a routine earnings 8-K at
60. The matching Form 4 for $286M followed two days later, so the 144-then-4
sequence works; the bands do not. Severe 8-K items are genuinely rare: 12 events
at 85+ in two years, against 362 earnings releases at 60.

## One bug, three times: matching text somebody else controls

Worth naming as a class rather than filing as three anecdotes, because the fix for
the class is different from the fix for any of them.

| What happened | How it showed up | Cost |
|---|---|---|
| `type=4` is a **prefix** match, so it also returned 424B2, 424B3 and 485APOS | Form 4 resolution sat at 42% | Found in an hour, by checking resolution rate against filings seen |
| A "President" rule matched **"Vice President"** | Senior-officer bonuses inflated | Found while reading real titles in fixtures |
| `SC 13G` became **`SCHEDULE 13G`** in 2025 | Nothing showed up at all | Nine months |

Every one is our code matching a string that SEC owns and can change without
telling anyone. The first two were *visible* -- wrong data arrived and looked
wrong. The third was invisible, because the query returned an empty list, and an
empty list is indistinguishable from a quiet week. That is the shape to fear.

Three defences now exist, in increasing order of how much they would have helped:

1. **Adapters declare the base forms they accept** and discard the rest after
   parsing. This handles prefix pollution -- the first bug -- and is why `type=144`
   does not pull in whatever else starts with those digits.
2. **The drought check** (`pipeline/watchdog.py`): a source producing nothing for a
   fortnight, against a historical rate that says it should, raises an alarm at
   severity 80. This is the general answer to "responding but dead", and it needed
   the backfill to know what normal looked like.
3. **The form census** (`adapters/form_census.py`): one request every fifteen
   minutes to the *unfiltered* index, normalising every form name to its family --
   `SC 13G` and `SCHEDULE 13G` are both `13G`, `4/A` is `4`. A family we already
   claim, arriving under a spelling we do not ask for, is a rename in progress.
   Checked against a live feed while building it: the census sees `SCHEDULE 13D/A`
   and `SCHEDULE 13G` plainly, which is exactly what nine months of 13D/G polling
   never saw. It would have turned nine months into a day.

The general lesson, for the next source: when a query returns nothing, that is data
about the query as much as about the world. Any adapter whose normal output is
non-zero should have a floor under it.

## Recalibration, measured (2026-09-29)

The corpus was built to judge the constants, and then it did. Before and after, over
the same 11,390 events and 496 market days:

| | flags before | after |
|---|---|---|
| Form 144 | 1,300 | **168** |
| Form 4 | 73 | **136** |
| 8-K | 747 | 747 |
| 13D/G | 254 | 254 |
| **per market day** | **4.8** | **2.6** |

Three changes, each argued from the data rather than from taste.

**Form 144 came off the threshold.** A routine insider notice scored exactly 30 --
the bar -- so 750 flags were one low-value form. The insider bonus dropped to 5 and
the plan discount deepened to -15. Size, concentration and crowds still flag; 1,136
notices went quiet.

**Large sales started scoring**, from the distribution rather than a round number:
3,051 real sales sit at p50 $1.1M, p90 $21.9M, ~p96 $50M, ~p99 $217M, max $1.5B. So
over $50M scores 45 and over $250M scores 60, with a -10 plan discount rather than
the buy side's -30, because a nine-figure scheduled sale is still a decision.

**Then the first cut of that was wrong, and the corpus said so immediately.** The
new top of the feed was eight consecutive Bezos filings, each a scheduled
billion-dollar Amazon sale. The large-sale tail is nearly all repeats -- Karp 12
filings, Bezos 12, Stevens 11, Samueli 7, with the top ten sellers accounting for
about two thirds of every large sale in two years. That is the Form 144 flood again
with bigger numbers. So repeats by the same insider inside ninety days are damped 25
points, which removed 44 flags and left the 64 that begin a run. The top of the list
is now unplanned sales -- a fund GP at $499M, Rakuten's CEO selling AST at $271M --
with scheduled programmes ranked beneath them. Two Bezos rows remain, thirteen
months apart, which is right: a new programme after a year is news again.

What makes this different from the first twenty constants is that each number now
has a measurement behind it and a command that re-measures it: `make corpus` for the
distribution, `python -m signals rescore --dry-run` to ask what a change would cost
before paying for it. The rescore is explicit and never automatic -- ingest never
rewrites history -- and both paths compute the repeat count with the same function,
because if they could disagree the corpus would stop being a baseline.
