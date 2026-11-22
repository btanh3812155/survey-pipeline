-- Database-backed logic/object module catalog. Python implementations remain code-owned.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('survey_pipeline_schema_migrations'));
SELECT set_config('survey_pipeline.migration_sha256', :'migration_sha256', true);
SELECT set_config('survey_pipeline.migration_007_sha256', :'migration_007_sha256', true);
DO $$
DECLARE recorded TEXT;
BEGIN
    IF to_regclass(format('%I.schema_migrations', current_schema())) IS NULL THEN
        RAISE EXCEPTION 'migration 008 requires migration 007';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='007';
    IF recorded IS DISTINCT FROM current_setting('survey_pipeline.migration_007_sha256') THEN
        RAISE EXCEPTION 'migration 008 requires matching migration 007';
    END IF;
    SELECT checksum INTO recorded FROM schema_migrations WHERE version='008';
    IF recorded IS NOT NULL AND recorded <> current_setting('survey_pipeline.migration_sha256') THEN
        RAISE EXCEPTION 'migration 008 checksum mismatch';
    END IF;
END
$$;
CREATE TABLE IF NOT EXISTS admin_module_registry (
    module_id VARCHAR(64) PRIMARY KEY CHECK (module_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    module_type VARCHAR(16) NOT NULL CHECK (module_type IN ('object','logic')),
    display_name VARCHAR(100) NOT NULL,
    module_version VARCHAR(32) NOT NULL,
    definition JSONB NOT NULL CHECK (jsonb_typeof(definition)='object'),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(64),
    updated_by VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_admin_module_registry_type_enabled
    ON admin_module_registry(module_type,enabled,module_id);
CREATE TABLE IF NOT EXISTS admin_module_registry_seed (
    singleton BOOLEAN PRIMARY KEY CHECK (singleton),
    seeded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO admin_module_registry(module_id,module_type,display_name,module_version,definition)
VALUES ('gsm-profile-screen','object','GSM profile screen','2026.10', $gsm$
{
  "version": 1,
  "id": "gsm-profile-screen",
  "label": "GSM profile screen",
  "description": "GSM driver profile and membership view.",
  "prompt": "Inspect the GSM driver profile image and extract visible membership, achievements, history, and name.",
  "objects": ["GSM driver profile screen"],
  "expected_fields": [
    {"key":"membership","label":"Membership","type":"string","required":false},
    {"key":"achievements","label":"Achievements","type":"array","required":true},
    {"key":"history","label":"History","type":"array","required":true},
    {"key":"name","label":"Name","type":"string","required":false},
    {"key":"version","label":"Version","type":"string","required":false},
    {"key":"app_name","label":"App name","type":"string","required":false}
  ],
  "customizable": [
    {"key":"version","label":"App version","type":"text","default":"2026.10"},
    {"key":"app_name","label":"App name","type":"text","default":"gsm"}
  ],
  "engines": [{"id":"required_fields","label":"Required fields","params":{"fields":["achievements","history"]}}],
  "extractor":"generic",
  "classify_role":"gsm_profile_screen",
  "json_output_instruction":true
}
$gsm$::jsonb)
ON CONFLICT (module_id) DO NOTHING;
INSERT INTO admin_module_registry(module_id,module_type,display_name,module_version,definition)
VALUES ('get-percentage','logic','get-percentage','1', $logic$
{
  "id":"get-percentage",
  "name":"get-percentage",
  "version":"1",
  "description":"Calculate target / total * 100; returns zero when total is zero.",
  "implementation":"get_percentage",
  "inputs":[
    {"name":"target","type":"integer","required":true},
    {"name":"total","type":"integer","required":true}
  ]
}
$logic$::jsonb)
ON CONFLICT (module_id) DO NOTHING;
INSERT INTO schema_migrations(version,checksum) VALUES ('008',:'migration_sha256')
    ON CONFLICT(version) DO NOTHING;
COMMIT;
