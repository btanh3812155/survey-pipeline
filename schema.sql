-- ====================================================================
-- PostgreSQL Master Schema for Survey Verification Pipeline
-- Scaled for 20,000+ submissions/day with pgvector and Outbox pattern
-- ====================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 1. Authorized Collaborators & RBAC Whitelist
CREATE TABLE IF NOT EXISTS authorized_users (
    telegram_user_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('COLLABORATOR', 'DRIVER', 'ADMIN')),
    driver_phone VARCHAR(20),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 1b. Pseudonymized Driver Identity Vault (Decree 13 / GDPR PII Isolation)
CREATE TABLE IF NOT EXISTS driver_identity_vault (
    driver_id VARCHAR(32) PRIMARY KEY, -- e.g. 'DRV-84920'
    driver_phone VARCHAR(20) UNIQUE NOT NULL,
    driver_name TEXT,
    is_scrubbed BOOLEAN NOT NULL DEFAULT FALSE,
    scrubbed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_driver_vault_phone ON driver_identity_vault (driver_phone);

-- 2. Master Survey Submissions
-- 2. Master Survey Submissions (Authoritative SSOT Record)
CREATE TABLE IF NOT EXISTS survey_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id VARCHAR(64) UNIQUE NOT NULL,
    form_id VARCHAR(64) NOT NULL DEFAULT 'income_survey',
    collaborator_id BIGINT REFERENCES authorized_users(telegram_user_id),
    ctv_code VARCHAR(64),
    driver_id VARCHAR(32) REFERENCES driver_identity_vault(driver_id),
    driver_phone VARCHAR(20), -- NULL for REJECTED submissions or scrubbed records
    driver_name TEXT,
    receipt_serial_no VARCHAR(64),
    store_tax_id VARCHAR(32),
    image_r2_url TEXT,
    signature_r2_url TEXT,
    gps_latitude DOUBLE PRECISION,
    gps_longitude DOUBLE PRECISION,
    gps_accuracy_meters REAL,
    image_dhash BIT(64),
    embedding vector(384),
    status VARCHAR(32) NOT NULL CHECK (status IN ('APPROVED', 'REJECTED', 'APPEAL_PENDING', 'MANUAL_REVIEW')),
    rejection_reason TEXT,
    -- Form specific survey fields
    consent_choice TEXT,
    vehicle_type VARCHAR(32),
    car_model VARCHAR(64),
    gsm_duration VARCHAR(64),
    gsm_contract_type VARCHAR(64),
    daily_trips INT,
    monthly_income_range VARCHAR(64),
    gsm_incentive_policy TEXT,
    membership_type VARCHAR(64),
    -- 5-week income statistics (October 2026)
    income_week_1_gross BIGINT DEFAULT 0,
    income_week_1_net BIGINT DEFAULT 0,
    income_week_1_trips INT DEFAULT 0,
    income_week_2_gross BIGINT DEFAULT 0,
    income_week_2_net BIGINT DEFAULT 0,
    income_week_2_trips INT DEFAULT 0,
    income_week_3_gross BIGINT DEFAULT 0,
    income_week_3_net BIGINT DEFAULT 0,
    income_week_3_trips INT DEFAULT 0,
    income_week_4_gross BIGINT DEFAULT 0,
    income_week_4_net BIGINT DEFAULT 0,
    income_week_4_trips INT DEFAULT 0,
    income_week_5_gross BIGINT DEFAULT 0,
    income_week_5_net BIGINT DEFAULT 0,
    income_week_5_trips INT DEFAULT 0,
    total_gross_revenue BIGINT DEFAULT 0,
    total_net_income BIGINT DEFAULT 0,
    total_trips INT DEFAULT 0,
    form_data JSONB NOT NULL DEFAULT '{}'::jsonb, -- Flexible payload supporting arbitrary form fields
    raw_extracted_facts JSONB,
    audit_seal_hash VARCHAR(64), -- Immutable SHA-256 seal (phone + ts + gps + signature)
    moderated_by_admin_id BIGINT,
    moderated_at TIMESTAMPTZ,
    moderation_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Auto-migration: ensure columns exist for existing databases
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS vehicle_type VARCHAR(32);
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS consent_choice TEXT;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS car_model VARCHAR(64);
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS gsm_duration VARCHAR(64);
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS gsm_contract_type VARCHAR(64);
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS daily_trips INT;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS monthly_income_range VARCHAR(64);
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS gsm_incentive_policy TEXT;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS membership_type VARCHAR(64);
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_1_gross BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_1_net BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_1_trips INT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_2_gross BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_2_net BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_2_trips INT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_3_gross BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_3_net BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_3_trips INT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_4_gross BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_4_net BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_4_trips INT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_5_gross BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_5_net BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS income_week_5_trips INT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS total_gross_revenue BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS total_net_income BIGINT DEFAULT 0;
ALTER TABLE survey_submissions ADD COLUMN IF NOT EXISTS total_trips INT DEFAULT 0;

-- Index for dynamic form queries
CREATE INDEX IF NOT EXISTS idx_surveys_form_id ON survey_submissions (form_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_surveys_ctv_code ON survey_submissions (ctv_code, created_at DESC);
-- Allows legitimate re-submissions of receipts previously rejected for blur/glare.
CREATE UNIQUE INDEX IF NOT EXISTS idx_surveys_serial_tax_approved 
ON survey_submissions (receipt_serial_no, store_tax_id) 
WHERE status = 'APPROVED' AND receipt_serial_no IS NOT NULL AND store_tax_id IS NOT NULL;

-- Fast index for collaborator lookup and reporting history
CREATE INDEX IF NOT EXISTS idx_surveys_collab_created 
ON survey_submissions (collaborator_id, created_at DESC);

-- HNSW Vector Index for background Tier 2 visual duplicate search
CREATE INDEX IF NOT EXISTS idx_surveys_embedding_hnsw 
ON survey_submissions USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 3. Transactional Outbox Table for Downstream Events (Decoupled Per-Flow Target)
CREATE TABLE IF NOT EXISTS outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(64) NOT NULL,
    flow_target VARCHAR(32) NOT NULL DEFAULT 'ALL' CHECK (flow_target IN ('ALL', 'BILLING', 'R2_UPLOAD', 'GDRIVE_EXPORT', 'LARK_SYNC', 'ZALO_ZNS')),
    aggregate_id VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')),
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    locked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_flow_pending 
ON outbox_events (flow_target, status, created_at ASC) 
WHERE status = 'PENDING';

-- 4. Collaborator Financial Billing Ledger
CREATE TABLE IF NOT EXISTS collaborator_billing_ledger (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collaborator_id BIGINT REFERENCES authorized_users(telegram_user_id),
    submission_id VARCHAR(64) UNIQUE NOT NULL REFERENCES survey_submissions(submission_id) ON DELETE RESTRICT,
    payout_amount_vnd INT NOT NULL DEFAULT 0,
    payout_status VARCHAR(32) NOT NULL DEFAULT 'UNPAID' CHECK (payout_status IN ('PENDING_PAYOUT', 'PAID', 'CANCELLED', 'UNPAID')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE collaborator_billing_ledger ALTER COLUMN collaborator_id DROP NOT NULL;
ALTER TABLE collaborator_billing_ledger ALTER COLUMN payout_amount_vnd SET DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_billing_collab_status 
ON collaborator_billing_ledger (collaborator_id, payout_status);

-- 5. Driver Persistent Vehicle Registry
CREATE TABLE IF NOT EXISTS driver_registered_vehicles (
    driver_phone VARCHAR(20) PRIMARY KEY,
    license_plate VARCHAR(32) NOT NULL,
    vehicle_type VARCHAR(16) NOT NULL CHECK (vehicle_type IN ('BIKE', 'CAR')),
    vehicle_color TEXT,
    vehicle_model TEXT,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_driver_plate 
ON driver_registered_vehicles (license_plate);

-- 6. Reference Database of Online Stock / Marketing Vehicle Images
CREATE TABLE IF NOT EXISTS online_stock_images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    image_title TEXT NOT NULL,
    image_dhash BIT(64) NOT NULL,
    source_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_dhash 
ON online_stock_images (image_dhash);

-- 7. Vehicle Change Audit & History Ledger
CREATE TABLE IF NOT EXISTS vehicle_change_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_phone VARCHAR(20) NOT NULL REFERENCES driver_registered_vehicles(driver_phone) ON DELETE CASCADE,
    previous_plate VARCHAR(32) NOT NULL,
    new_plate VARCHAR(32) NOT NULL,
    previous_vehicle_type VARCHAR(16) NOT NULL,
    new_vehicle_type VARCHAR(16) NOT NULL,
    changed_by_admin_id BIGINT,
    change_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_veh_change_phone 
ON vehicle_change_history (driver_phone, created_at DESC);

-- 8. Immutable System-Wide Audit Log (Decree 13 / DPIA Compliance)
CREATE TABLE IF NOT EXISTS system_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hop_number INT, -- Sequence hop: 1=INGEST, 2=NORMALIZER, 3=ANTI_FRAUD, 4=AI_VISION, 5=EVALUATOR, 6=TG_NOTIFY, 7=OUTBOX, 8=DOWNSTREAM
    hop_name VARCHAR(64), -- 'HOP_1_INGEST', 'HOP_2_NORMALIZER', etc.
    component_name VARCHAR(64) NOT NULL,
    action VARCHAR(64) NOT NULL,
    entity_type VARCHAR(32) NOT NULL, -- 'SURVEY', 'DRIVER', 'VEHICLE', 'PAYOUT', 'CHAT'
    entity_id VARCHAR(64) NOT NULL,
    actor_type VARCHAR(16) NOT NULL CHECK (actor_type IN ('SYSTEM', 'DRIVER', 'ADMIN')),
    actor_id VARCHAR(64), -- Telegram ID, Driver ID, or 'SYSTEM'
    ip_address VARCHAR(45),
    details JSONB NOT NULL,
    tamper_evident_hash VARCHAR(64) -- SHA256(prev_hash + timestamp + action + details)
);

CREATE INDEX IF NOT EXISTS idx_audit_entity 
ON system_audit_log (entity_type, entity_id, event_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_audit_component 
ON system_audit_log (component_name, event_timestamp DESC);

-- 9. Internal Telegram Conversation Logger (Decree 13 Compliant Audit Trail)
CREATE TABLE IF NOT EXISTS telegram_chat_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    role VARCHAR(16) NOT NULL CHECK (role IN ('COLLABORATOR', 'DRIVER', 'ADMIN', 'SYSTEM')),
    direction VARCHAR(8) NOT NULL CHECK (direction IN ('INBOUND', 'OUTBOUND')),
    message_text TEXT,
    is_scrubbed BOOLEAN NOT NULL DEFAULT FALSE,
    scrubbed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_history_user 
ON telegram_chat_history (telegram_user_id, created_at DESC);

-- 10. Automated 36-Hour Ephemeral Message Deletion Queue (Telegram Privacy)
CREATE TABLE IF NOT EXISTS telegram_sent_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delete_after TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '36 hours'),
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_telegram_pending_deletions 
ON telegram_sent_messages (delete_after ASC) 
WHERE is_deleted = FALSE;

-- 11. Cross-Device Collaborator Survey Drafts (Per-Link Persistence)
CREATE TABLE IF NOT EXISTS survey_drafts (
    ctv_slug VARCHAR(64) PRIMARY KEY,
    ctv_id VARCHAR(64) NOT NULL,
    form_id VARCHAR(64) NOT NULL DEFAULT 'income_survey',
    draft_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_survey_drafts_ctv 
ON survey_drafts (ctv_id, updated_at DESC);
