-- Multi-role support on authorized_users while keeping legacy `role` in sync.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_015_sha256', :'migration_015_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 016 requires migration 015';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='015';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_015_sha256') THEN
        RAISE EXCEPTION 'migration 016 requires matching migration 015';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='016';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 016 checksum mismatch';
    END IF;
END
$$;

ALTER TABLE authorized_users ADD COLUMN IF NOT EXISTS roles TEXT[];
UPDATE authorized_users SET roles = ARRAY[role]::text[] WHERE roles IS NULL OR cardinality(roles) = 0;
ALTER TABLE authorized_users ALTER COLUMN roles SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'authorized_users_roles_check'
          AND conrelid = format('%I.authorized_users', current_schema())::regclass
    ) THEN
        ALTER TABLE authorized_users ADD CONSTRAINT authorized_users_roles_check
        CHECK (cardinality(roles) >= 1 AND roles <@ ARRAY['COLLABORATOR', 'DRIVER', 'ADMIN']::text[]);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION sync_authorized_user_roles() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE normalized TEXT[];
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.role IS DISTINCT FROM OLD.role AND NEW.roles IS NOT DISTINCT FROM OLD.roles THEN
        NEW.roles := ARRAY[NEW.role]::text[];
    ELSIF TG_OP = 'INSERT' AND NEW.roles IS NULL THEN
        NEW.roles := ARRAY[NEW.role]::text[];
    ELSE
        SELECT COALESCE(
            array_agg(r ORDER BY array_position(ARRAY['ADMIN', 'COLLABORATOR', 'DRIVER'], r), r),
            '{}'::text[]
        ) INTO normalized
        FROM (SELECT DISTINCT unnest(NEW.roles) AS r) s;
        NEW.roles := normalized;
        IF cardinality(NEW.roles) >= 1 AND NEW.roles[1] = ANY(ARRAY['ADMIN', 'COLLABORATOR', 'DRIVER']) THEN
            NEW.role := NEW.roles[1];
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER authorized_users_sync_roles
BEFORE INSERT OR UPDATE ON authorized_users
FOR EACH ROW EXECUTE FUNCTION sync_authorized_user_roles();

INSERT INTO schema_migrations(version,checksum) VALUES ('016',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
