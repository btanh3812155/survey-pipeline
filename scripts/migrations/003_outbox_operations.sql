-- Additive worker polling evidence; never enables workers or producers.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_002_sha256', :'migration_002_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 003 requires migration 002';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='002';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_002_sha256') THEN
        RAISE EXCEPTION 'migration 003 requires matching migration 002';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='003';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 003 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS outbox_worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    last_poll_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    build_id TEXT NOT NULL
);
CREATE TEMP TABLE migration_003_contract (
    worker_id TEXT PRIMARY KEY,
    last_poll_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    build_id TEXT NOT NULL
) ON COMMIT DROP;
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_attribute WHERE attrelid='outbox_worker_heartbeats'::regclass AND attnum>0 AND NOT attisdropped) <> 4
       OR EXISTS (
        SELECT 1 FROM pg_attribute e
        LEFT JOIN pg_attribute a ON a.attrelid='outbox_worker_heartbeats'::regclass AND a.attname=e.attname AND NOT a.attisdropped
        LEFT JOIN pg_attrdef ed ON ed.adrelid=e.attrelid AND ed.adnum=e.attnum
        LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
        WHERE e.attrelid='pg_temp.migration_003_contract'::regclass AND e.attnum>0 AND NOT e.attisdropped
          AND (a.attnum IS NULL OR a.atttypid IS DISTINCT FROM e.atttypid OR a.attnotnull IS DISTINCT FROM e.attnotnull
               OR pg_get_expr(ad.adbin,ad.adrelid) IS DISTINCT FROM pg_get_expr(ed.adbin,ed.adrelid))
       ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conrelid='outbox_worker_heartbeats'::regclass AND contype='p'
          AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='outbox_worker_heartbeats'::regclass AND attname='worker_id')]::smallint[]
       ) THEN
        RAISE EXCEPTION 'migration 003: incompatible worker heartbeat definition';
    END IF;
END
$$;
INSERT INTO schema_migrations(version,checksum) VALUES ('003', :'migration_sha256') ON CONFLICT(version) DO NOTHING;
COMMIT;
