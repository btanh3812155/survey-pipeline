-- Persisted trash compaction runs and scheduler report history.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_013_sha256', :'migration_013_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 014 requires migration 013';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='013';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_013_sha256') THEN
        RAISE EXCEPTION 'migration 014 requires matching migration 013';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='014';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 014 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS trash_compaction_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    status VARCHAR(16) NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','busy','failed')),
    kept INT NOT NULL DEFAULT 0,
    removed INT NOT NULL DEFAULT 0,
    protected_recent INT NOT NULL DEFAULT 0,
    error TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_trash_compaction_runs_finished
    ON trash_compaction_runs(finished_at DESC);
INSERT INTO schema_migrations(version,checksum) VALUES ('014',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
