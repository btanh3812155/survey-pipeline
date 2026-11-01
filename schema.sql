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
    role TEXT NOT NULL CHECK (role IN ('COLLABORATOR', 'ADMIN')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Master Survey Submissions
-- 2. Master Survey Submissions (Authoritative SSOT Record)
CREATE TABLE IF NOT EXISTS survey_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id VARCHAR(64) UNIQUE NOT NULL,
    collaborator_id BIGINT NOT NULL REFERENCES authorized_users(telegram_user_id),
    driver_phone VARCHAR(20),
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
    raw_extracted_facts JSONB,
    audit_seal_hash VARCHAR(64), -- Immutable SHA-256 seal (phone + ts + gps + signature)
    moderated_by_admin_id BIGINT,
    moderated_at TIMESTAMPTZ,
    moderation_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
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

-- 3. Transactional Outbox Table for Downstream Events
CREATE TABLE IF NOT EXISTS outbox_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')),
    retry_count INT NOT NULL DEFAULT 0,
    locked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending 
ON outbox_events (status, created_at ASC) 
WHERE status = 'PENDING';

-- 4. Collaborator Financial Billing Ledger
CREATE TABLE IF NOT EXISTS collaborator_billing_ledger (
    transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collaborator_id BIGINT NOT NULL REFERENCES authorized_users(telegram_user_id),
    submission_id VARCHAR(64) UNIQUE NOT NULL REFERENCES survey_submissions(submission_id) ON DELETE RESTRICT,
    payout_amount_vnd INT NOT NULL DEFAULT 25000,
    payout_status VARCHAR(32) NOT NULL DEFAULT 'PENDING_PAYOUT' CHECK (payout_status IN ('PENDING_PAYOUT', 'PAID', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
    component_name VARCHAR(64) NOT NULL, -- 'INGRESS', 'NORMALIZER', 'ANTI_FRAUD', 'AI_VISION', 'EVALUATOR', 'MODERATION', 'OUTBOX'
    action VARCHAR(64) NOT NULL, -- 'WEBHOOK_RECEIVED', 'EXIF_SCANNED', 'AI_EXTRACTED', 'SURVEY_REJECTED', 'ADMIN_OVERRIDE', 'ZNS_SENT'
    entity_type VARCHAR(32) NOT NULL, -- 'SURVEY', 'DRIVER', 'VEHICLE', 'PAYOUT'
    entity_id VARCHAR(64) NOT NULL,
    actor_type VARCHAR(16) NOT NULL CHECK (actor_type IN ('SYSTEM', 'DRIVER', 'ADMIN')),
    actor_id VARCHAR(64), -- Telegram ID or 'SYSTEM'
    ip_address VARCHAR(45),
    details JSONB NOT NULL,
    tamper_evident_hash VARCHAR(64) -- SHA256(prev_hash + timestamp + action + details)
);

CREATE INDEX IF NOT EXISTS idx_audit_entity 
ON system_audit_log (entity_type, entity_id, event_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_audit_component 
ON system_audit_log (component_name, event_timestamp DESC);
