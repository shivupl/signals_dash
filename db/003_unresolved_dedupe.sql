-- The unresolved queue is a work list, not a growth log.
--
-- Without a key it was re-recording the same filing on every poll: 16 distinct
-- entities became 6,668 rows in four hours, about 37,000 a day. Keying it on the
-- filing's accession makes "insert on conflict do nothing" collapse the repeats,
-- exactly as it does for `event`.

alter table unresolved add column if not exists external_id text;

update unresolved
set external_id = coalesce(payload->>'accession', 'legacy-' || id::text)
where external_id is null;

-- Collapse what accumulated before the constraint existed, keeping the first
-- sighting of each -- seen_at should say when it was first seen, not last.
delete from unresolved a
using unresolved b
where a.id > b.id
  and a.source = b.source
  and a.external_id = b.external_id;

alter table unresolved alter column external_id set not null;

create unique index if not exists unresolved_source_external_id_idx
  on unresolved (source, external_id);
