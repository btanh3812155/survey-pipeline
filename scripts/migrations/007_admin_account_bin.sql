-- Admin roles, recovery challenges and shared rate limits, and the 30-day bin.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_006_sha256', :'migration_006_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 007 requires migration 006';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '006';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_006_sha256') THEN
        RAISE EXCEPTION 'migration 007 requires matching migration 006';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '007';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 007 checksum mismatch';
    END IF;
END
$$;
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(64);
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'reviewer';
ALTER TABLE admin_accounts ADD COLUMN IF NOT EXISTS recovery_email VARCHAR(254);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM schema_migrations WHERE version='007') THEN
        ALTER TABLE admin_accounts DROP CONSTRAINT IF EXISTS admin_accounts_role_check;
        ALTER TABLE admin_accounts ALTER COLUMN role SET DEFAULT 'reviewer';
        UPDATE admin_accounts SET role='superadmin',
            recovery_email=COALESCE(NULLIF(btrim(recovery_email),''),'admin@trustbridgevn.com'),
            updated_at=clock_timestamp()
        WHERE username='admin' AND role IN ('admin','reviewer','superadmin')
            AND is_active AND deleted_at IS NULL;
        UPDATE admin_accounts SET role='reviewer',updated_at=clock_timestamp() WHERE role='admin';
        ALTER TABLE admin_accounts ADD CONSTRAINT admin_accounts_role_check
            CHECK (role IN ('superadmin','manager','reviewer','viewer'));
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS admin_recovery_challenges (
    id UUID PRIMARY KEY,
    username VARCHAR(64) NOT NULL REFERENCES admin_accounts(username) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('reset','email')),
    state_hash BYTEA NOT NULL CHECK (octet_length(state_hash)=32),
    old_code_hash BYTEA NOT NULL CHECK (octet_length(old_code_hash)=32),
    new_code_hash BYTEA CHECK (octet_length(new_code_hash)=32),
    new_email VARCHAR(254),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    consumed_at TIMESTAMPTZ,
    UNIQUE (username,purpose),
    CHECK ((purpose='email' AND new_email IS NOT NULL AND new_code_hash IS NOT NULL)
        OR (purpose='reset' AND new_email IS NULL AND new_code_hash IS NULL))
);

CREATE TABLE IF NOT EXISTS admin_recovery_limits (
    bucket BYTEA PRIMARY KEY CHECK (octet_length(bucket)=32),
    hits SMALLINT NOT NULL CHECK (hits > 0),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_recovery_limits_expiry ON admin_recovery_limits(expires_at);

INSERT INTO schema_migrations(version, checksum) VALUES ('007', :'migration_sha256') ON CONFLICT(version) DO NOTHING;
COMMIT;
