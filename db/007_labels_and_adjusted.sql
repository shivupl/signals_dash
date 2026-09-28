-- Your verdict on a flag, so daily reading becomes ground truth.
--
-- Added before the backfill rather than after it: altering a table with a few
-- hundred rows is instant, and the same statement against two years of history is
-- a maintenance window. Nothing writes this yet -- the UI control comes later --
-- but the column being here means labels can start accruing the day it does.
--
-- Deliberately a boolean and not a score. "Was this worth reading" is the
-- judgement the feed exists to get right, and a three-point scale invites
-- deliberation over a question that should take a keystroke.
alter table event add column if not exists label      boolean;
alter table event add column if not exists labeled_at timestamptz;

-- Partial: labelled rows are the rare case and the only ones queried this way.
create index if not exists event_label_idx on event (label) where label is not null;

-- The split-adjusted close, beside the raw one.
--
-- price_daily.close is the price printed that day, which is what a flag's price_at
-- means and what the UI shows. It is the wrong number for a forward return: a
-- 10-for-1 split reads as a 90% collapse, and dividends bias the result. Both are
-- stored because both are needed -- raw for "what did it cost", adjusted for
-- "what did it return".
alter table price_daily add column if not exists adj_close numeric;
