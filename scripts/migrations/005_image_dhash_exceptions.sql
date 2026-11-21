-- Add the perceptual hash to the time-bound image exception so re-photographed
-- screenshots can bypass all three inter/intra tiers, not just the embedding tier.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_004_sha256', :'migration_004_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 005 requires migration 004';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '004';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_004_sha256') THEN
        RAISE EXCEPTION 'migration 005 requires matching migration 004';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '005';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 005 checksum mismatch';
    END IF;
END
$$;
ALTER TABLE image_dinov2_exceptions ADD COLUMN IF NOT EXISTS image_dhash BIT(64);
INSERT INTO schema_migrations(version, checksum) VALUES ('005', :'migration_sha256') ON CONFLICT(version) DO NOTHING;
COMMIT;
