-- The five tables, as specified. Three things carry most of the weight:
--
--   unique (source, external_id) is the ENTIRE dedupe strategy. The same index
--   is polled every couple of seconds and the same filing comes back hundreds of
--   times; the constraint absorbs it, and "insert ... on conflict do nothing
--   returning id" reports newness in one round trip.
--
--   price_at is stamped when the flag fires. It cannot be reconstructed later,
--   and without it the Phase 2 outcome log is guesswork.
--
--   occurred_at vs ingested_at is the latency metric. Both from day one.

create table if not exists company (
  id         bigserial primary key,
  cik        text unique,
  ticker     text,
  name       text not null,
  watched    boolean not null default false
);

-- The alias graph. Phase 1 sources all carry hard identifiers, so this looks
-- like over-engineering today; it is built now because WARN notices and court
-- dockets in Phase 2 name subsidiaries and holding companies, and retrofitting
-- the indirection later means rewriting every adapter.
create table if not exists company_alias (
  company_id bigint not null references company(id) on delete cascade,
  kind       text not null,          -- 'cik' | 'ticker' | 'legal_name'
  value      text not null,
  primary key (kind, value)
);
create index if not exists company_alias_company_idx on company_alias (company_id);

create table if not exists event (
  id           bigserial primary key,
  -- Nullable on purpose: watchdog system events and market-wide (M/MWC) halts
  -- are real events with no company.
  company_id   bigint references company(id),
  source       text not null,
  event_type   text not null,
  occurred_at  timestamptz not null,   -- when the market could have known
  ingested_at  timestamptz not null default now(),
  external_id  text not null,
  summary      text,
  score        int not null default 0,
  price_at     numeric,
  payload      jsonb not null,
  url          text,
  unique (source, external_id)
);
create index if not exists event_occurred_idx on event (occurred_at desc);
create index if not exists event_company_occurred_idx on event (company_id, occurred_at desc);
create index if not exists event_score_idx on event (score desc) where score >= 30;

-- A work list, not a bin. Events that fail resolution are still stored.
create table if not exists unresolved (
  id          bigserial primary key,
  source      text not null,
  raw_name    text not null,
  payload     jsonb not null,
  seen_at     timestamptz not null default now(),
  resolved    boolean not null default false
);

create table if not exists price_daily (
  company_id bigint references company(id) on delete cascade,
  d          date,                    -- MARKET date in ET, not occurred_at::date
  close      numeric,
  primary key (company_id, d)
);
