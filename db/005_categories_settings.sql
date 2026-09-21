-- A normalized event category, so the UI can filter "insider sells" or "officer
-- changes" across sources without knowing each source's own vocabulary. Filled at
-- insert; existing rows are backfilled by `python -m signals migrate`, in Python,
-- so the mapping lives in exactly one place (signals/categories.py).
alter table event add column if not exists category text;
create index if not exists event_category_idx on event (category, occurred_at desc);

-- Runtime settings. The flag threshold used to be an env var read at startup;
-- here it can change without a restart, and without touching stored history --
-- every event keeps the score it was given regardless of what gets flagged.
create table if not exists setting (
  key        text primary key,
  value      text not null,
  updated_at timestamptz not null default now()
);

-- The first watchdog wrote a new row per episode, and on a host that kept dozing
-- left nine alarms for one fact. Those rows describe the old behaviour, not
-- anything that happened to a source; the rewritten watchdog keeps one row per
-- outage and updates it in place.
delete from event where source = 'system' and (external_id like 'stale:%' or external_id like 'recovered:%');
