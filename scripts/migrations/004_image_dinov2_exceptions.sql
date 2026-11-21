-- Time-bound per-form image exceptions for the inter-submission fraud tiers.
-- Rows are ignored automatically once expires_at passes; no cleanup job is required.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_003_sha256', :'migration_003_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 004 requires migration 003';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '003';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_003_sha256') THEN
        RAISE EXCEPTION 'migration 004 requires matching migration 003';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '004';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 004 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS image_dinov2_exceptions (
    id BIGSERIAL PRIMARY KEY,
    form_id VARCHAR(64) NOT NULL,
    image_sha256 CHAR(64) NOT NULL,
    embedding vector(384),
    note TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (form_id, image_sha256)
);
CREATE INDEX IF NOT EXISTS idx_image_dinov2_exceptions_active
    ON image_dinov2_exceptions (form_id, expires_at);
INSERT INTO schema_migrations(version, checksum) VALUES ('004', :'migration_sha256') ON CONFLICT(version) DO NOTHING;
COMMIT;
