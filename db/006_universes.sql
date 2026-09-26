-- Which companies belong to which monitor. Membership is not exclusive: 16 of
-- the original 40 are also S&P 500 members.
--
-- company.watched keeps its meaning -- "ingest this company" -- and is derived
-- from this table at seed time. The worker still reads one flat set of CIKs, so
-- adding a universe changes what a view can show, not how ingest works.
create table if not exists universe_member (
  company_id bigint not null references company(id) on delete cascade,
  universe   text   not null,
  primary key (company_id, universe)
);

create index if not exists universe_member_universe_idx
  on universe_member (universe, company_id);

-- Everything watched today is the 'core' monitor.
insert into universe_member (company_id, universe)
select id, 'core' from company where watched
on conflict do nothing;
