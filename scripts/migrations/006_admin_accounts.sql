-- Database-backed admin accounts for the web console.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_005_sha256', :'migration_005_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 006 requires migration 005';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '005';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_005_sha256') THEN
        RAISE EXCEPTION 'migration 006 requires matching migration 005';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '006';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 006 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS admin_accounts (
    username VARCHAR(64) PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO schema_migrations(version, checksum) VALUES ('006', :'migration_sha256') ON CONFLICT(version) DO NOTHING;
COMMIT;
