-- DB-side trash, transaction audit, and Drive name-to-ID ledger.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_011_sha256', :'migration_011_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 012 requires migration 011';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='011';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_011_sha256') THEN
        RAISE EXCEPTION 'migration 012 requires matching migration 011';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='012';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 012 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS trash_items (
    id UUID PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL CHECK (entity_type IN ('form','submission')),
    entity_id VARCHAR(64) NOT NULL,
    label TEXT,
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    deleted_by VARCHAR(64),
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    retention_tier VARCHAR(16) NOT NULL DEFAULT 'all'
        CHECK (retention_tier IN ('all','hourly','daily','weekly','monthly','expired')),
    restored_at TIMESTAMPTZ,
    purged_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_trash_items_entity
    ON trash_items(entity_type, entity_id, deleted_at DESC);
CREATE INDEX IF NOT EXISTS idx_trash_items_expiry
    ON trash_items(expires_at)
    WHERE restored_at IS NULL AND purged_at IS NULL;
CREATE TABLE IF NOT EXISTS trash_transactions (
    id BIGSERIAL PRIMARY KEY,
    trash_id UUID REFERENCES trash_items(id) ON DELETE SET NULL,
    action VARCHAR(32) NOT NULL,
    actor VARCHAR(64),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_trash_transactions_trash
    ON trash_transactions(trash_id, created_at DESC);
CREATE TABLE IF NOT EXISTS drive_name_ledger (
    id BIGSERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    object_name TEXT NOT NULL,
    drive_id TEXT NOT NULL UNIQUE,
    parent_drive_id TEXT,
    parent_name TEXT,
    mime_type TEXT,
    target_id TEXT,
    state VARCHAR(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (state IN ('ACTIVE','TRASHED','PURGED')),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_action VARCHAR(32),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_drive_name_ledger_entity
    ON drive_name_ledger(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_drive_name_ledger_state
    ON drive_name_ledger(state, last_seen_at DESC);
INSERT INTO schema_migrations(version,checksum) VALUES ('012',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
