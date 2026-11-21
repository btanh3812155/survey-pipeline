-- Wave 4A migration 002. apply_migration.sh supplies both SHA-256 values.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_001_sha256', :'migration_001_sha256', true);

DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 002 requires migration 001';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '001';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_001_sha256') THEN
        RAISE EXCEPTION 'migration 002 requires migration 001 with expected checksum (recorded %, expected %)',
            recorded, current_setting('survey_pipeline.migration_001_sha256');
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version = '002';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 002 checksum mismatch (recorded %, provided %)',
            recorded, current_setting('survey_pipeline.migration_sha256');
    END IF;
END
$$;

ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS max_attempts INT NOT NULL DEFAULT 5;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS terminal_reason TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS action_key TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS aggregate_kind TEXT NOT NULL DEFAULT 'submission';
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS result JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS work_owner TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS decision_version BIGINT NOT NULL DEFAULT 0;

-- Compare normalized expressions and widen only the recognized legacy target CHECK.
CREATE TEMP TABLE migration_002_legacy (
    flow_target VARCHAR(32) CHECK (flow_target IN ('ALL', 'BILLING', 'R2_UPLOAD', 'GDRIVE_EXPORT', 'LARK_SYNC', 'ZALO_ZNS'))
) ON COMMIT DROP;
CREATE TEMP TABLE migration_002_outbox (
    flow_target VARCHAR(32) NOT NULL DEFAULT 'ALL' CONSTRAINT outbox_events_flow_target_wave4_check
        CHECK (flow_target IN ('ALL', 'BILLING', 'R2_UPLOAD', 'GDRIVE_EXPORT', 'LARK_SYNC', 'ZALO_ZNS', 'TELEGRAM_NOTIFY')),
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), claimed_by TEXT,
    max_attempts INT NOT NULL DEFAULT 5 CONSTRAINT outbox_events_max_attempts_wave4_check CHECK (max_attempts BETWEEN 1 AND 5),
    terminal_reason TEXT, lease_until TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0 CONSTRAINT outbox_events_attempts_wave4_check CHECK (attempts BETWEEN 0 AND 5),
    action_key TEXT,
    aggregate_kind TEXT NOT NULL DEFAULT 'submission' CONSTRAINT outbox_events_aggregate_kind_wave4_check
        CHECK (aggregate_kind IN ('submission', 'lark_event', 'drive_sync')),
    result JSONB NOT NULL DEFAULT '{}'::jsonb
) ON COMMIT DROP;
CREATE TEMP TABLE migration_002_submission (
    work_owner TEXT NOT NULL DEFAULT 'legacy' CONSTRAINT survey_submissions_work_owner_wave4_check CHECK (work_owner IN ('legacy', 'outbox_v2')),
    decision_version BIGINT NOT NULL DEFAULT 0 CONSTRAINT survey_submissions_decision_version_wave4_check CHECK (decision_version >= 0)
) ON COMMIT DROP;

DO $$
DECLARE
    legacy_expr TEXT;
    target_expr TEXT;
    existing RECORD;
    found_count INT := 0;
    legacy_name NAME;
BEGIN
    SELECT pg_get_expr(conbin, conrelid) INTO legacy_expr FROM pg_constraint
    WHERE conrelid = 'pg_temp.migration_002_legacy'::regclass AND contype = 'c';
    SELECT pg_get_expr(conbin, conrelid) INTO target_expr FROM pg_constraint
    WHERE conrelid = 'pg_temp.migration_002_outbox'::regclass AND conname = 'outbox_events_flow_target_wave4_check';
    FOR existing IN SELECT conname, convalidated, conislocal, coninhcount, pg_get_expr(conbin, conrelid) AS definition
        FROM pg_constraint c
        WHERE c.conrelid = 'outbox_events'::regclass AND c.contype = 'c'
          AND (SELECT attnum FROM pg_attribute WHERE attrelid = c.conrelid AND attname = 'flow_target') = ANY(c.conkey)
    LOOP
        found_count := found_count + 1;
        IF NOT existing.convalidated OR NOT existing.conislocal OR existing.coninhcount <> 0
           OR existing.definition NOT IN (legacy_expr, target_expr) THEN
            RAISE EXCEPTION 'migration 002: unsupported flow_target constraint %', existing.conname;
        END IF;
        IF existing.definition = legacy_expr THEN
            legacy_name := existing.conname;
        END IF;
    END LOOP;
    IF found_count <> 1 THEN
        RAISE EXCEPTION 'migration 002: expected one known flow_target constraint, found %', found_count;
    END IF;
    IF legacy_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE outbox_events DROP CONSTRAINT %I', legacy_name);
    ELSIF existing.conname <> 'outbox_events_flow_target_wave4_check' THEN
        RAISE EXCEPTION 'migration 002: unexpected name for widened flow_target constraint: %', existing.conname;
    END IF;
END
$$;

DO $$
DECLARE expected RECORD;
BEGIN
    FOR expected IN
        SELECT conname, pg_get_constraintdef(oid) AS definition,
            CASE WHEN conrelid = 'pg_temp.migration_002_outbox'::regclass THEN 'outbox_events'::regclass
                 ELSE 'survey_submissions'::regclass END AS target
        FROM pg_constraint
        WHERE conrelid IN ('pg_temp.migration_002_outbox'::regclass, 'pg_temp.migration_002_submission'::regclass)
          AND contype = 'c'
    LOOP
        IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = expected.target AND conname = expected.conname) THEN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = expected.target AND conname = expected.conname
                           AND contype = 'c' AND convalidated AND pg_get_constraintdef(oid) = expected.definition) THEN
                RAISE EXCEPTION 'migration 002: incompatible constraint %', expected.conname;
            END IF;
        ELSE
            CASE expected.conname
                WHEN 'outbox_events_flow_target_wave4_check' THEN
                    ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_flow_target_wave4_check
                        CHECK (flow_target IN ('ALL', 'BILLING', 'R2_UPLOAD', 'GDRIVE_EXPORT', 'LARK_SYNC', 'ZALO_ZNS', 'TELEGRAM_NOTIFY'));
                WHEN 'outbox_events_max_attempts_wave4_check' THEN
                    ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_max_attempts_wave4_check CHECK (max_attempts BETWEEN 1 AND 5);
                WHEN 'outbox_events_attempts_wave4_check' THEN
                    ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_attempts_wave4_check CHECK (attempts BETWEEN 0 AND 5);
                WHEN 'outbox_events_aggregate_kind_wave4_check' THEN
                    ALTER TABLE outbox_events ADD CONSTRAINT outbox_events_aggregate_kind_wave4_check CHECK (aggregate_kind IN ('submission', 'lark_event', 'drive_sync'));
                WHEN 'survey_submissions_work_owner_wave4_check' THEN
                    ALTER TABLE survey_submissions ADD CONSTRAINT survey_submissions_work_owner_wave4_check CHECK (work_owner IN ('legacy', 'outbox_v2'));
                WHEN 'survey_submissions_decision_version_wave4_check' THEN
                    ALTER TABLE survey_submissions ADD CONSTRAINT survey_submissions_decision_version_wave4_check CHECK (decision_version >= 0);
                ELSE
                    RAISE EXCEPTION 'migration 002: unknown contract %', expected.conname;
            END CASE;
        END IF;
    END LOOP;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_events_action
ON outbox_events (aggregate_kind, aggregate_id, event_type, flow_target, action_key)
WHERE action_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS outbox_delivery_attempts (
    event_id UUID NOT NULL REFERENCES outbox_events(id),
    claim_token TEXT NOT NULL,
    attempt_number INT NOT NULL CHECK (attempt_number BETWEEN 1 AND 5),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_started BOOLEAN NOT NULL DEFAULT FALSE,
    finished_at TIMESTAMPTZ,
    outcome TEXT,
    reason TEXT,
    receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (event_id, claim_token)
);

CREATE TEMP TABLE migration_002_attempt (
    event_id UUID NOT NULL, claim_token TEXT NOT NULL,
    attempt_number INT NOT NULL CONSTRAINT outbox_delivery_attempts_attempt_number_check CHECK (attempt_number BETWEEN 1 AND 5),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), request_started BOOLEAN NOT NULL DEFAULT FALSE,
    finished_at TIMESTAMPTZ, outcome TEXT, reason TEXT, receipt JSONB NOT NULL DEFAULT '{}'::jsonb
) ON COMMIT DROP;
DO $$
DECLARE pair RECORD;
BEGIN
    FOR pair IN SELECT * FROM (VALUES
        ('outbox_events'::regclass, 'pg_temp.migration_002_outbox'::regclass),
        ('survey_submissions'::regclass, 'pg_temp.migration_002_submission'::regclass),
        ('outbox_delivery_attempts'::regclass, 'pg_temp.migration_002_attempt'::regclass)
    ) AS contracts(actual_table, expected_table) LOOP
        IF EXISTS (
            SELECT 1 FROM pg_attribute expected
            LEFT JOIN pg_attribute actual ON actual.attrelid=pair.actual_table
                AND actual.attname=expected.attname AND NOT actual.attisdropped
            LEFT JOIN pg_attrdef ed ON ed.adrelid=expected.attrelid AND ed.adnum=expected.attnum
            LEFT JOIN pg_attrdef ad ON ad.adrelid=actual.attrelid AND ad.adnum=actual.attnum
            WHERE expected.attrelid=pair.expected_table AND expected.attnum>0 AND NOT expected.attisdropped
              AND (actual.attnum IS NULL OR actual.atttypid IS DISTINCT FROM expected.atttypid
                OR actual.atttypmod IS DISTINCT FROM expected.atttypmod
                OR actual.attnotnull IS DISTINCT FROM expected.attnotnull
                OR pg_get_expr(ad.adbin, ad.adrelid) IS DISTINCT FROM pg_get_expr(ed.adbin, ed.adrelid))
        ) THEN
            RAISE EXCEPTION 'migration 002: incompatible columns in %', pair.actual_table;
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_attribute WHERE attrelid='outbox_delivery_attempts'::regclass AND attnum>0 AND NOT attisdropped) <> 9
       OR NOT EXISTS (SELECT 1 FROM pg_constraint actual JOIN pg_constraint expected
                      ON expected.conrelid='pg_temp.migration_002_attempt'::regclass
                      AND expected.conname='outbox_delivery_attempts_attempt_number_check'
                      WHERE actual.conrelid='outbox_delivery_attempts'::regclass
                        AND actual.conname=expected.conname AND actual.contype='c' AND actual.convalidated
                        AND pg_get_constraintdef(actual.oid)=pg_get_constraintdef(expected.oid)) THEN
        RAISE EXCEPTION 'migration 002: incompatible attempt log schema';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        WHERE i.indrelid='outbox_delivery_attempts'::regclass AND i.indisprimary AND i.indisvalid
          AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
               FROM unnest(i.indkey) WITH ORDINALITY k(num,ord)
               JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num)
              = ARRAY['event_id','claim_token']::text[]
    ) THEN
        RAISE EXCEPTION 'migration 002: incompatible attempt log primary key';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid='outbox_delivery_attempts'::regclass AND c.contype='f' AND c.convalidated
          AND c.confrelid='outbox_events'::regclass AND c.confdeltype='a' AND c.confupdtype='a'
          AND NOT c.condeferrable
          AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=c.conrelid AND attname='event_id')]::smallint[]
          AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid=c.confrelid AND attname='id')]::smallint[]
    ) THEN
        RAISE EXCEPTION 'migration 002: incompatible attempt log event reference';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
        JOIN pg_am am ON am.oid=idx.relam
        WHERE i.indrelid='outbox_events'::regclass AND idx.relname='uq_outbox_events_action'
          AND i.indisunique AND i.indisvalid AND i.indisready AND i.indnkeyatts=5 AND i.indnatts=5
          AND i.indexprs IS NULL AND am.amname='btree'
          AND pg_get_expr(i.indpred,i.indrelid)='(action_key IS NOT NULL)'
          AND (SELECT array_agg(a.attname::text ORDER BY k.ord)
               FROM unnest(i.indkey) WITH ORDINALITY k(num,ord)
               JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num)
              = ARRAY['aggregate_kind','aggregate_id','event_type','flow_target','action_key']::text[]
    ) THEN
        RAISE EXCEPTION 'migration 002: incompatible action index';
    END IF;
END
$$;

INSERT INTO schema_migrations (version, checksum) VALUES ('002', :'migration_sha256')
ON CONFLICT (version) DO NOTHING;
COMMIT;
