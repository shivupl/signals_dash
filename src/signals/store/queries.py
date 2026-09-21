"""Every SQL statement in the project.

Keeping them here means the shape of the database is readable in one file, and
the handful of statements that carry real semantics -- the dedupe insert, the
distinct-insider window, the latency percentiles -- are not scattered through
adapter code.
"""

from __future__ import annotations

from typing import Final

# The dedupe contract. The same filing is seen on every poll for as long as it
# stays in the 100-entry window -- often fifty times or more -- and this absorbs
# all of it. RETURNING id fires only on a genuine insert, so "was this new?" and
# "store it" are one round trip rather than a read followed by a write.
INSERT_EVENT: Final[str] = """
insert into event (
  company_id, source, event_type, occurred_at, external_id,
  summary, score, payload, url, category
)
values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
on conflict (source, external_id) do nothing
returning id
"""

FIND_COMPANY_BY_ALIAS: Final[str] = """
select c.id, c.cik, c.ticker, c.name, c.watched
from company_alias a
join company c on c.id = a.company_id
where a.kind = $1 and a.value = $2
"""

WATCHED_COMPANY_IDS: Final[str] = "select id from company where watched"

# The Form 4 adapter needs CIKs, not ids: it decides whether a filing is worth a
# document fetch from the issuer CIK in the index, before anything is stored.
WATCHED_CIKS: Final[str] = """
select distinct a.value
from company_alias a
join company c on c.id = a.company_id
where a.kind = 'cik' and c.watched
"""

# Keyed on the filing, so re-polling the same window does not append a row each
# time. Same dedupe contract as `event`, for the same reason.
RECORD_UNRESOLVED: Final[str] = """
insert into unresolved (source, external_id, raw_name, payload)
values ($1, $2, $3, $4::jsonb)
on conflict (source, external_id) do nothing
"""

# Only ever fills a blank: price_at is "the price when the flag fired", and a
# later write would quietly turn it into something else.
STAMP_PRICE_AT: Final[str] = """
update event set price_at = $2 where id = $1 and price_at is null
"""

UPSERT_PRICE_DAILY: Final[str] = """
insert into price_daily (company_id, d, close) values ($1, $2, $3)
on conflict (company_id, d) do update set close = excluded.close
"""

# The close on an event's own market date, or the nearest one before it (a filing
# accepted on a Saturday is priced at Friday's close).
CLOSE_ON_OR_BEFORE: Final[str] = """
select close from price_daily
where company_id = $1 and d <= $2
order by d desc limit 1
"""

SET_NEXT_EARNINGS: Final[str] = "update company set next_earnings = $2 where id = $1"

WATCHED_COMPANIES: Final[str] = """
select id, cik, ticker, name, watched from company
where watched and ticker is not null order by ticker
"""

# In-place update for a row whose facts legitimately change over time -- an open
# outage whose duration counts up. Distinct from MERGE_PAYLOAD, which is guarded
# to apply exactly once.
UPDATE_PAYLOAD: Final[str] = """
update event set payload = payload || $3::jsonb, summary = coalesce($4, summary)
where source = $1 and external_id = $2
returning id
"""

UPDATE_SCORE: Final[str] = """
update event set score = $2, payload = jsonb_set(payload, '{score_parts}', $3::jsonb)
where id = $1
"""

# Back-annotate a halt row with its resumption. The guard makes the merge a
# genuine no-op after the first time, so repeated polling neither rewrites the
# row nor re-publishes it.
MERGE_PAYLOAD: Final[str] = """
update event
set payload = payload || $3::jsonb
where source = $1 and external_id = $2 and not (payload ? $4)
returning id
"""

# Distinct insiders, by CIK. Matching on name would split "John A. Smith" from
# "SMITH JOHN A" and fabricate a cluster out of one person.
WATCHED_TICKERS: Final[str] = """
select distinct a.value
from company_alias a
join company c on c.id = a.company_id
where a.kind = 'ticker' and c.watched
"""

DISTINCT_P_BUYERS: Final[str] = """
select count(distinct cik) as buyers
from event e, jsonb_array_elements_text(e.payload -> 'insider_ciks') as cik
where e.company_id = $1
  and e.source = 'edgar_form4'
  and e.event_type = 'form4_buy'
  and e.occurred_at >= $2
"""

# Candidates for retroactive promotion. The score_parts guard is what makes
# re-running promotion free rather than merely harmless.
EVENTS_MISSING_CLUSTER_BONUS: Final[str] = """
select id, company_id, source, event_type, occurred_at, ingested_at,
       external_id, summary, score, price_at, payload, url
from event
where company_id = $1
  and source = 'edgar_form4'
  and event_type = 'form4_buy'
  and occurred_at >= $2
  and payload -> 'score_parts' -> 'cluster' is null
"""

_EVENT_COLUMNS: Final[str] = """
  e.id, e.company_id, e.source, e.event_type, e.occurred_at, e.ingested_at,
  e.external_id, e.summary, e.score, e.price_at, e.payload, e.url, e.category,
  c.ticker, c.name as company_name,
  (select p.close from price_daily p
    where p.company_id = e.company_id order by p.d desc limit 1) as price_now
"""

# Every filter is optional and collapses to "true" when unset, so one statement
# serves every combination without string building. Multi-value filters are
# arrays; NULL means "no filter". System events are excluded unless asked for --
# they are the pipeline talking about itself, not a company event, and belong in
# the status strip rather than among the flags.
_FEED_WHERE: Final[str] = """
where e.score >= $1
  and ($2::timestamptz is null or e.occurred_at >= $2)
  and ($3::timestamptz is null or e.occurred_at <  $3)
  and ($4::text[] is null or upper(c.ticker) = any($4))
  and ($5::text[] is null or e.source = any($5))
  and ($6::text[] is null or e.category = any($6))
  and ($7::boolean or e.source <> 'system')
"""

FEED: Final[str] = f"""
select {_EVENT_COLUMNS}
from event e
left join company c on c.id = e.company_id
{_FEED_WHERE}
order by e.occurred_at desc, e.id desc
limit $8 offset $9
"""

# What the header reports. Same predicate as the feed, so the two cannot disagree.
FEED_COUNT: Final[str] = f"""
select count(*) from event e
left join company c on c.id = e.company_id
{_FEED_WHERE}
"""

# Open outages first, then the last day's resolved ones and notices.
SYSTEM_STATUS: Final[str] = f"""
select {_EVENT_COLUMNS}
from event e
left join company c on c.id = e.company_id
where e.source = 'system'
  and (e.payload->>'state' = 'open' or e.occurred_at > now() - interval '24 hours')
order by (e.payload->>'state' = 'open') desc, e.occurred_at desc
limit 30
"""

UNCATEGORIZED: Final[str] = """
select id, source, event_type, payload from event where category is null limit 5000
"""

SET_CATEGORY: Final[str] = "update event set category = $2 where id = $1"

COMPANY_BY_TICKER: Final[str] = """
select c.id, c.cik, c.ticker, c.name, c.watched, c.next_earnings
from company_alias a join company c on c.id = a.company_id
where a.kind = 'ticker' and a.value = upper($1)
"""

COMPANY_PRICES: Final[str] = """
select d, close from price_daily
where company_id = $1 and d >= current_date - $2::int
order by d
"""

# Raw material for the insider table. Aggregated in Python: a filing can name
# several owners, and the grouping key is the insider's CIK, never the name.
COMPANY_FORM4: Final[str] = """
select occurred_at, url, score, payload
from event
where company_id = $1 and source = 'edgar_form4' and occurred_at > now() - $2::interval
order by occurred_at desc
"""

# Filings that failed to resolve but plausibly belong here: the raw name starts
# with this company's own leading words. Deliberately conservative, and labelled
# "possible" wherever it is shown.
POSSIBLE_ALIASES: Final[str] = """
select raw_name, count(*) as filings, max(seen_at) as last_seen
from unresolved
where not resolved and lower(raw_name) like lower($1) || '%'
group by raw_name order by filings desc limit 10
"""

# Form 4 rows stored before sale values were recorded.
FORM4_MISSING_SALES: Final[str] = """
select external_id, url from event
where source = 'edgar_form4' and url is not null
  and (not (payload ? 'sale_value') or payload->>'headline' like 'Form 4 filed by%')
"""

GET_SETTING: Final[str] = "select value from setting where key = $1"

PUT_SETTING: Final[str] = """
insert into setting (key, value) values ($1, $2)
on conflict (key) do update set value = excluded.value, updated_at = now()
"""

GET_EVENT: Final[str] = f"""
select {_EVENT_COLUMNS}
from event e
left join company c on c.id = e.company_id
where e.id = $1
"""

# The rail: every watched company with its week, whether or not it had events.
WATCHLIST: Final[str] = """
select c.id, c.ticker, c.name, c.next_earnings,
       (select p.close from price_daily p
         where p.company_id = c.id order by p.d desc limit 1) as last_price,
       -- the most recent close at least a week back: "this week's" baseline
       (select p.close from price_daily p
         where p.company_id = c.id and p.d <= current_date - 7
         order by p.d desc limit 1) as week_ago_price,
       count(e.id) filter (where e.occurred_at > now() - interval '7 days') as flags,
       coalesce(max(e.score) filter (where e.occurred_at > now() - interval '7 days'), 0) as top,
       max(e.occurred_at) as last_event_at
from company c
left join event e on e.company_id = c.id and e.score >= $1
  -- The same source and category filters as the feed. Without them the header
  -- could say "1 flag" beside a rail listing three flagged companies.
  and ($2::text[] is null or e.source = any($2))
  and ($3::text[] is null or e.category = any($3))
where c.watched
group by c.id, c.ticker, c.name, c.next_earnings
order by flags desc, top desc, c.ticker
"""

# The number worth quoting: detection lag, per source, measured not estimated.
# Only events the worker was already running to see.
#
# The first poll of a 100-entry window returns filings accepted hours earlier,
# and measuring those reports the age of the backlog rather than detection lag --
# a number the dashboard would be stating while knowing it to be wrong. Events
# carry `live_capture` set at ingest; replayed fixtures never carry it.
LATENCY_PERCENTILES: Final[str] = """
select source,
       count(*) as events,
       percentile_cont(0.5) within group (
         order by extract(epoch from (ingested_at - occurred_at))
       ) as p50,
       percentile_cont(0.95) within group (
         order by extract(epoch from (ingested_at - occurred_at))
       ) as p95
from event
where ingested_at > now() - $1::interval
  and occurred_at <= ingested_at
  and payload->>'live_capture' = 'true'
group by source
order by source
"""

#: How many events the latency figures deliberately ignore, so the dashboard can
#: say so rather than quietly under-report its sample size.
EXCLUDED_FROM_LATENCY: Final[str] = """
select count(*) as excluded
from event
where ingested_at > now() - $1::interval
  and coalesce(payload->>'live_capture', 'false') <> 'true'
"""

FLAGS_PER_DAY: Final[str] = """
select date_trunc('day', occurred_at) as day, source, count(*) as flags
from event
where score >= $1 and occurred_at > now() - $2::interval
group by 1, 2
order by 1 desc, 2
"""

COUNT_UNRESOLVED: Final[str] = "select count(*) as open from unresolved where not resolved"
