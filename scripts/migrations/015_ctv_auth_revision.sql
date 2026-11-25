-- Durable CTV session and pending OTP revocation across identity restoration.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_014_sha256', :'migration_014_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 015 requires migration 014';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='014';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_014_sha256') THEN
        RAISE EXCEPTION 'migration 015 requires matching migration 014';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='015';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 015 checksum mismatch';
    END IF;
END
$$;
ALTER TABLE ctv_directory ADD COLUMN IF NOT EXISTS auth_revision UUID NOT NULL DEFAULT gen_random_uuid();

CREATE OR REPLACE FUNCTION rotate_ctv_auth_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.code, NEW.phone, NEW.telegram_user_id, NEW.is_active)
        IS DISTINCT FROM ROW(OLD.code, OLD.phone, OLD.telegram_user_id, OLD.is_active) THEN
        NEW.auth_revision := gen_random_uuid();
    ELSE
        NEW.auth_revision := OLD.auth_revision;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER ctv_directory_auth_revision
BEFORE UPDATE ON ctv_directory
FOR EACH ROW EXECUTE FUNCTION rotate_ctv_auth_revision();

INSERT INTO schema_migrations(version,checksum) VALUES ('015',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
