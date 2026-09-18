-- Additive indexes for the hot read paths. Split from the schema so the schema
-- file stays a faithful copy of the spec.

-- The cluster query: distinct insiders with a P-buy for one company in 30 days.
-- Runs on every Form 4 insert, so it is worth an index.
create index if not exists event_form4_cluster_idx
  on event (company_id, occurred_at desc)
  where source = 'edgar_form4' and event_type = 'form4_buy';

-- /feed filtered by source, and /stats grouping by source.
create index if not exists event_source_occurred_idx on event (source, occurred_at desc);

-- The unresolved work list only ever queries the open items.
create index if not exists unresolved_open_idx on unresolved (seen_at desc) where not resolved;
