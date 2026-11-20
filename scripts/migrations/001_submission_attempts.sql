-- Wave 3 migration 001. psql -X -v ON_ERROR_STOP=1 -v migration_sha256=<sha>
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $$
DECLARE existing_checksum TEXT;
BEGIN
    SELECT checksum INTO existing_checksum FROM schema_migrations WHERE version = '001';
    IF existing_checksum IS NOT NULL
       AND existing_checksum <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 001 checksum mismatch (recorded %, provided %)',
            existing_checksum, current_setting('survey_pipeline.migration_sha256');
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS submission_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    submission_id VARCHAR(64),
    response_json JSONB,
    state VARCHAR(16) NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_submission_idempotency_expiry
ON submission_idempotency (expires_at)
WHERE expires_at IS NOT NULL;
ALTER TABLE survey_submissions
    ADD COLUMN IF NOT EXISTS media_manifest JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE survey_submissions
    ADD COLUMN IF NOT EXISTS media_version BIGINT NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'submission_idempotency'
          AND (
              (column_name = 'scope' AND data_type <> 'text')
              OR (column_name = 'idempotency_key' AND data_type <> 'text')
              OR (column_name = 'request_fingerprint' AND (data_type <> 'character' OR character_maximum_length <> 64))
              OR (column_name = 'submission_id' AND (data_type <> 'character varying' OR character_maximum_length <> 64))
              OR (column_name = 'response_json' AND data_type <> 'jsonb')
              OR (column_name = 'state' AND (data_type <> 'character varying' OR character_maximum_length <> 16))
              OR (column_name = 'scope' AND is_nullable <> 'NO')
              OR (column_name = 'idempotency_key' AND is_nullable <> 'NO')
              OR (column_name = 'request_fingerprint' AND is_nullable <> 'NO')
              OR (column_name = 'submission_id' AND is_nullable <> 'YES')
              OR (column_name = 'response_json' AND is_nullable <> 'YES')
              OR (column_name = 'state' AND is_nullable <> 'NO')
              OR (column_name = 'created_at' AND (data_type <> 'timestamp with time zone' OR is_nullable <> 'NO' OR column_default IS NULL OR column_default NOT LIKE '%now()%'))
              OR (column_name = 'expires_at' AND data_type <> 'timestamp with time zone')
          )
    ) THEN
        RAISE EXCEPTION 'migration 001: incompatible submission_idempotency column definition';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'submission_idempotency'
          AND column_name IN ('scope','idempotency_key','request_fingerprint','submission_id','response_json','state','created_at','expires_at')) <> 8 THEN
        RAISE EXCEPTION 'migration 001: incomplete submission_idempotency table definition';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'submission_idempotency'::regclass AND i.indisprimary
        GROUP BY i.indexrelid
        HAVING array_agg(a.attname::text ORDER BY a.attnum) = ARRAY['scope','idempotency_key']::text[]
    ) THEN
        RAISE EXCEPTION 'migration 001: submission_idempotency primary key mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = 'submission_idempotency'
          AND indexname = 'idx_submission_idempotency_expiry'
          AND indexdef ILIKE '%USING btree (expires_at)%'
          AND indexdef ILIKE '%WHERE (expires_at IS NOT NULL)%'
          AND indexdef NOT ILIKE '%INCLUDE%'
    ) THEN
        RAISE EXCEPTION 'migration 001: expiry index mismatch';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'survey_submissions'
          AND column_name = 'media_manifest' AND data_type = 'jsonb'
          AND is_nullable = 'NO' AND column_default LIKE '%[]%'
    ) OR NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'survey_submissions'
          AND column_name = 'media_version' AND data_type = 'bigint'
          AND is_nullable = 'NO' AND column_default = '1'
    ) THEN
        RAISE EXCEPTION 'migration 001: incompatible media columns';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'schema_migrations'
          AND column_name IN ('version','checksum','applied_at')) <> 3
       OR EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'schema_migrations'
          AND ((column_name = 'version' AND (data_type <> 'text' OR is_nullable <> 'NO'))
            OR (column_name = 'checksum' AND (data_type <> 'text' OR is_nullable <> 'NO'))
            OR (column_name = 'applied_at' AND (data_type <> 'timestamp with time zone' OR is_nullable <> 'NO' OR column_default IS NULL OR column_default NOT LIKE '%now()%')))
       ) THEN
        RAISE EXCEPTION 'migration 001: incompatible schema_migrations definition';
    END IF;

    -- Compare server-normalized expressions against the migration-owned shape.
    -- Sampling rejected values cannot prove that no extra state is accepted.
    CREATE TEMP TABLE migration_001_state_contract (
        state VARCHAR(16) CHECK (state IN ('RESERVED', 'COMMITTED'))
    ) ON COMMIT DROP;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint actual
        WHERE actual.conrelid = 'submission_idempotency'::regclass
          AND actual.contype = 'c' AND actual.convalidated
          AND pg_get_expr(actual.conbin, actual.conrelid) = (
              SELECT pg_get_expr(expected.conbin, expected.conrelid)
              FROM pg_constraint expected
              WHERE expected.conrelid = 'pg_temp.migration_001_state_contract'::regclass
                AND expected.contype = 'c'
          )
    ) THEN
        RAISE EXCEPTION 'migration 001: submission_idempotency state constraint mismatch';
    END IF;

    -- Keep checking that the existing column defaults allow both valid inserts.
    BEGIN
        INSERT INTO submission_idempotency
            (scope, idempotency_key, request_fingerprint, state)
        VALUES
            ('__migration_probe_' || pg_backend_pid(), 'reserved', repeat('0', 64), 'RESERVED'),
            ('__migration_probe_' || pg_backend_pid(), 'committed', repeat('0', 64), 'COMMITTED');
        DELETE FROM submission_idempotency
        WHERE scope = '__migration_probe_' || pg_backend_pid();
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'migration 001: idempotency state defaults/constraint reject valid inserts';
    END;
END
$$;

INSERT INTO schema_migrations (version, checksum)
VALUES ('001', :'migration_sha256')
ON CONFLICT (version) DO NOTHING;
COMMIT;
