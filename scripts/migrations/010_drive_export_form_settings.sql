-- Per-form output overrides for Google Drive clients.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_009_sha256', :'migration_009_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 010 requires migration 009';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='009';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_009_sha256') THEN
        RAISE EXCEPTION 'migration 010 requires matching migration 009';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='010';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 010 checksum mismatch';
    END IF;
END
$$;
ALTER TABLE drive_export_clients
    ADD COLUMN IF NOT EXISTS form_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
INSERT INTO schema_migrations(version,checksum) VALUES ('010',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
