-- Enforced per-CTV survey assignments (authorization boundary, separate from ctv_form_links metadata).
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_016_sha256', :'migration_016_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 017 requires migration 016';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='016';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_016_sha256') THEN
        RAISE EXCEPTION 'migration 017 requires matching migration 016';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='017';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 017 checksum mismatch';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS ctv_form_assignments (
    ctv_code TEXT NOT NULL REFERENCES ctv_directory(code) ON DELETE CASCADE,
    form_id TEXT NOT NULL,
    assigned_by TEXT,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ctv_code, form_id)
);
CREATE INDEX IF NOT EXISTS idx_ctv_form_assignments_form ON ctv_form_assignments (form_id);

-- Backfill all active non-test CTVs onto the live Nov GSM campaign form so rollout does not lock out active CTVs.
INSERT INTO ctv_form_assignments (ctv_code, form_id, assigned_by)
SELECT code, 'TB_GSM_2026_Nov_Test', 'migration_017'
FROM ctv_directory
WHERE is_active = TRUE
  AND UPPER(code) NOT LIKE '%TEST%'
  AND UPPER(COALESCE(display_name, '')) NOT LIKE '%TEST%'
ON CONFLICT (ctv_code, form_id) DO NOTHING;

INSERT INTO schema_migrations(version,checksum) VALUES ('017',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
