-- The next scheduled earnings date, surfaced in the watchlist rail. It is the one
-- scheduled event that changes how everything else reads: an insider buy a week
-- before earnings is a different fact from one a week after.
alter table company add column if not exists next_earnings date;
