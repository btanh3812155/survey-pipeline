-- Retune DB trash retention: hourly, four-hourly + last 10, daily, weekly, monthly, semiannual forever.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_012_sha256', :'migration_012_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 013 requires migration 012';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='012';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_012_sha256') THEN
        RAISE EXCEPTION 'migration 013 requires matching migration 012';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='013';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 013 checksum mismatch';
    END IF;
END
$$;
ALTER TABLE trash_items DROP CONSTRAINT IF EXISTS trash_items_retention_tier_check;
ALTER TABLE trash_items ALTER COLUMN expires_at DROP NOT NULL;
UPDATE trash_items SET expires_at=NULL, retention_tier=CASE
    WHEN retention_tier='expired' THEN 'semiannual'
    WHEN retention_tier='all' THEN 'hourly'
    ELSE retention_tier
END;
ALTER TABLE trash_items ADD CONSTRAINT trash_items_retention_tier_check
    CHECK (retention_tier IN ('hourly','four_hourly','daily','weekly','monthly','semiannual'));
INSERT INTO schema_migrations(version,checksum) VALUES ('013',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
