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
CREATE TABLE IF NOT EXISTS survey_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id VARCHAR(64) UNIQUE NOT NULL,
    collaborator_id BIGINT NOT NULL REFERENCES authorized_users(telegram_user_id),
    receipt_serial_no VARCHAR(64),
    store_tax_id VARCHAR(32),
    image_r2_url TEXT,
    image_dhash BIT(64),
    embedding vector(384),
    status VARCHAR(32) NOT NULL CHECK (status IN ('APPROVED', 'REJECTED', 'MANUAL_REVIEW')),
    rejection_reason TEXT,
    raw_extracted_facts JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Crucial: Partial Unique Index strictly for APPROVED submissions!
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
