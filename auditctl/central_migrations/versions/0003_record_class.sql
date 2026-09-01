-- Admit `decision` alongside `observation`.
--
-- The original CHECK pinned this column to a single value, enforcing the rule that
-- AuditContext.as_record states: auditctl records conformance, it does not state
-- desired state. An owner ruling on 2026-09-01 makes this store the home of the
-- settlement spine's Decision as well as its EvidenceSet, so a judgement is now
-- admissible here.
--
-- The vocabulary stays closed. The point of the original constraint was that this
-- store must not silently accumulate statements of desired state, and an open column
-- would give that back by accident. A reader must still be able to separate what
-- happened from what someone concluded about it, so this stays a checked column
-- rather than becoming free text.
--
-- Additive and non-destructive: every existing row is 'observation' and satisfies the
-- widened constraint, so no row is rewritten and no backfill is required.
--
-- The old constraint is found by catalog lookup rather than by name. Postgres names an
-- inline column CHECK `<table>_<column>_check`, but that is a default rather than a
-- guarantee, and dropping the wrong name with IF EXISTS would leave the original check
-- in force while the new one was added beside it -- decisions would still be rejected,
-- and the migration would report success. Failing to find it is better than that.
DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT con.conname INTO constraint_name
    FROM pg_constraint con
    JOIN pg_attribute att
      ON att.attrelid = con.conrelid
     AND att.attnum = ANY (con.conkey)
    WHERE con.conrelid = '__SCHEMA__.ingest_observation'::regclass
      AND con.contype = 'c'
      AND att.attname = 'record_class'
      AND array_length(con.conkey, 1) = 1;

    IF constraint_name IS NULL THEN
        RAISE EXCEPTION 'no single-column CHECK constraint found on ingest_observation.record_class';
    END IF;

    EXECUTE format(
        'ALTER TABLE __SCHEMA__.ingest_observation DROP CONSTRAINT %I',
        constraint_name
    );
END
$$;

ALTER TABLE __SCHEMA__.ingest_observation
    ADD CONSTRAINT ingest_observation_record_class_check
    CHECK (record_class IN ('observation', 'decision'));
