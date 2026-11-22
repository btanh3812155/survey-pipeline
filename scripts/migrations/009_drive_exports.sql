-- Google Drive export settings and per-client access configuration.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_008_sha256', :'migration_008_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 009 requires migration 008';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='008';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_008_sha256') THEN
        RAISE EXCEPTION 'migration 009 requires matching migration 008';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='009';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 009 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS drive_export_settings (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    master_folder_name VARCHAR(100) NOT NULL DEFAULT 'Master Export',
    client_folder_name VARCHAR(100) NOT NULL DEFAULT 'Client Exports',
    internal_folder_name VARCHAR(100) NOT NULL DEFAULT 'Internal Exports',
    images_folder_name VARCHAR(100) NOT NULL DEFAULT 'Images Master',
    updated_by VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_run_at TIMESTAMPTZ,
    last_report JSONB
);
INSERT INTO drive_export_settings(singleton) VALUES (TRUE) ON CONFLICT (singleton) DO NOTHING;
CREATE TABLE IF NOT EXISTS drive_export_clients (
    slug VARCHAR(64) PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    display_name VARCHAR(100) NOT NULL,
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('client','internal')),
    category VARCHAR(100),
    folder_name VARCHAR(100) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    date_from DATE,
    date_to DATE,
    form_ids TEXT[] NOT NULL DEFAULT '{}',
    fields TEXT[] NOT NULL DEFAULT '{submission_id,form_id,status,created_at,vehicle_type,total_gross_revenue,total_net_income,total_trips}',
    statuses TEXT[] NOT NULL DEFAULT '{APPROVED}',
    vehicle_types TEXT[] NOT NULL DEFAULT '{}',
    include_images BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(64),
    updated_by VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (kind='client' OR (category IS NOT NULL AND btrim(category) <> '')),
    CHECK (date_to IS NULL OR date_from IS NULL OR date_to >= date_from)
);
CREATE INDEX IF NOT EXISTS idx_drive_export_clients_enabled
    ON drive_export_clients(enabled, kind, display_name);
INSERT INTO schema_migrations(version,checksum) VALUES ('009',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
