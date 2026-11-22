-- Check engines become first-class admin module records.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_010_sha256', :'migration_010_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 011 requires migration 010';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='010';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_010_sha256') THEN
        RAISE EXCEPTION 'migration 011 requires matching migration 010';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='011';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 011 checksum mismatch';
    END IF;
END
$$;
ALTER TABLE admin_module_registry
    DROP CONSTRAINT IF EXISTS admin_module_registry_module_type_check;
ALTER TABLE admin_module_registry
    ADD CONSTRAINT admin_module_registry_module_type_check
    CHECK (module_type IN ('object','logic','engine'));
CREATE TABLE IF NOT EXISTS admin_module_engine_seed (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    seeded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO schema_migrations(version,checksum) VALUES ('011',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
