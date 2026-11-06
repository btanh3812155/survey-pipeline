# Survey Verification Pipeline: Master Execution, Migration & Expansion Roadmap Plan

**Document Classification:** Authoritative Strategic Roadmap, Rollout Battle Plan & Operational Expansion Plan  
**System Baseline:** Survey Verification Pipeline v3.0.0  
**Target Infrastructure:** DigitalOcean SGP1 (Singapore) + Cloudflare R2 + PostgreSQL (pgvector) + Redis  
**Nominal Service Level Agreement (SLA):** Synchronous review loop < 2,500 ms (p50), < 3,800 ms (p99)  
**Target Throughput Scale:** 2,000 -> 20,000 -> 50,000+ daily submissions  

---

## 1. Executive Summary & Evolution Plan

### 1.1 The Architecture Problem Statement
The original field survey verification system operated on an un-orchestrated chain of Google Forms, ByteDance Lark Base (Bitable) forms, and an n8n automation engine. In field testing across VinFast / GSM / Xanh SM electric bike and car drivers in Vietnam, this legacy architecture experienced critical failure modes:
1. **Severe Latency Bottlenecks (30s – 2m):** n8n processed large multi-megabyte mobile photos sequentially as in-memory base64 strings in Node.js, choking event loops and garbage collection.
2. **Synchronous Coupling:** Immediate collaborator responses were blocked by downstream side-effects (uploading full-res photos to Google Drive, appending to spreadsheets, and issuing accounting entries).
3. **ByteDance Lark Base Attachment Friction:** Ephemeral `tenant_access_token` requirements and non-static CDN redirect URLs introduced multiple round-trips before reading a single image byte.
4. **Client-Side Failure Modes:** Unhandled EXIF orientation flags flipped receipts sideways, while RGBA/CMYK color profile mismatches crashed image libraries during JPG/PNG conversions.

### 1.2 Core Architectural Mandate
The restructured pipeline enforces strict **SLA isolation**:
- **Synchronous Ingress Path (< 2.5s SLA):** Executes exclusively memory-buffered media normalization, fast Laplacian blur pre-filtering, 64-bit indexed perceptual hash duplicate detection, driver vehicle binding verification, and dual-model AI vision transcription.
- **Asynchronous Transactional Outbox:** All heavy downstream tasks (customer messaging, financial accounting ledgers, collaborator payroll, analytics data warehouse streaming, and cold storage archival) are completely decoupled via PostgreSQL `outbox_events` and Redis event queues.

```mermaid
flowchart TD
    subgraph SynchronousLoop ["Phase 1 & 2: Fast Ingress & Validation Loop (< 2.5s SLA)"]
        A[Mobile WebRTC Form / Lark Webhook] --> B[FastAPI Gateway]
        B --> C[Memory Normalizer: EXIF transpose & RGB flatten]
        C --> D{Blur Variance Check}
        D -- Blurry --> E[Immediate Rejection Notice to Telegram / Web]
        D -- Sharp --> F[Indexed pHash bitwise query in PostgreSQL]
        F -- Duplicate --> E
        F -- Unique --> G[Dual-Model Vision AI: Gemini Flash / DeepSeek]
        G --> H[Deterministic Compliance Evaluator]
        H --> I[Atomic DB Commit: survey_submissions + outbox_events]
        I --> J[Instant Approved / Rejection Response to Collaborator]
    end

    subgraph AsynchronousOutbox ["Phase 3: Decoupled Downstream Expansion Subsystems"]
        I -.->|Atomic Commit| K[(outbox_events table)]
        K --> L[Outbox Worker: SKIP LOCKED]
        L --> M[(Redis Streams / Event Bus)]
        M --> N[Subsystem A: Customer Outreach & Zalo ZNS]
        M --> O[Subsystem B: Financial Ledger & Billing]
        M --> P[Subsystem C: BI Analytics & Warehouse]
        M --> Q[Subsystem D: DINOv2 Vector Fraud Engine]
        M --> R[Subsystem E: Cloudflare R2 Cold Archive]
    end
```

---

## 2. The 3-Phase Execution Battle Plan & Status Audit

```mermaid
gantt
    title Pipeline Migration, Hardening & Expansion Roadmap
    dateFormat YYYY-MM-DD
    axisFormat %m/%d

    section Phase 1: Stabilization
    Lark API token caching & memory streaming     :done, p1_1, 2026-10-25, 2026-10-28
    EXIF auto-rotation & RGBA flattening         :done, p1_2, 2026-10-26, 2026-10-29
    WebRTC multi-slot continuous camera           :done, p1_3, 2026-10-28, 2026-10-31

    section Phase 2: Performance & Security
    Fast Laplacian blur pre-filtering             :done, p2_1, 2026-10-29, 2026-11-01
    PostgreSQL BIT(64) Hamming duplicate queries  :done, p2_2, 2026-10-30, 2026-11-02
    Dual-model AI vision (Gemini / DeepSeek)      :done, p2_3, 2026-10-31, 2026-11-03
    Deterministic compliance evaluator             :done, p2_4, 2026-11-01, 2026-11-04
    PII hashing & Decree 13 anonymization         :done, p2_5, 2026-11-03, 2026-11-05
    Server-side IP geolocation fallback           :done, p2_6, 2026-11-04, 2026-11-06

    section Phase 3: Downstream Expansion
    Transactional outbox & background worker      :done, p3_1, 2026-11-02, 2026-11-06
    Zalo ZNS & customer notification dispatch     :active, p3_2, 2026-11-07, 2026-11-15
    Collaborator payout & customer billing ledger :active, p3_3, 2026-11-10, 2026-11-20
    ClickHouse / BI analytics warehouse streaming :p3_4, 2026-11-18, 2026-11-30
    DINOv2 deep visual embeddings in pgvector     :p3_5, 2026-11-25, 2026-12-10
```

### 2.1 Phase 1 Status: Media Normalization & Lark Ingestion (100% Complete)
- **Zero Disk Buffer Streaming:** Raw multipart uploads stream into `io.BytesIO()`; no ephemeral disk files.
- **EXIF Transposition:** `ImageOps.exif_transpose` corrects mobile camera rotation tags before hashing.
- **Color Space Flattening:** Strips RGBA/CMYK channels and pastes onto solid white RGB canvas, resolving JPEG mode crashes.
- **Continuous Multi-Slot Camera:** Keeps WebRTC video stream active across multi-slot captures (`1 *`, `2 *`), automatically advancing without freezing the viewfinder.

### 2.2 Phase 2 Status: Latency Crushing & Privacy Hardening (100% Complete)
- **Laplacian Blur Pre-Check:** Rejects blurred images in `< 25ms` before invoking LLM inference.
- **Indexed Bitwise Hamming Query:** PostgreSQL `BIT(64)` duplicate search executes in `< 15ms`.
- **Dual-Model Vision AI:** Primary Gemini Flash vision extracts strict JSON facts in `~1.8s - 2.5s`; DeepSeek structured fallback provides high-availability failover.
- **Deterministic Rule Evaluator:** Prompt injection surface reduced to zero; arithmetic consistency enforced deterministically.
- **Privacy & PII Protection:** HMAC-SHA256 phone/plate hashing; pseudonymous `driver_tag` primary keys; history API locked behind admin authentication (HTTP 401 Unauthorized for public access).
- **Resilient Geolocation:** Browser permission persistence, 15s timeout, and 30ms server-side IP fallback (`GET /api/v1/geo/ip-fallback`) resolving domestic Vietnamese DNS stalls.

### 2.3 Phase 3 Status: Decoupled Outbox Event Bus (Core Live, Subsystems Expanding)
- **Transactional Outbox Table:** Masters table `survey_submissions` and `outbox_events` are written in a single atomic transaction.
- **Outbox Worker Daemon:** Continuous worker consumes pending events via `SELECT ... FOR UPDATE SKIP LOCKED` and dispatches to background targets.

---

## 3. Downstream Expansion Plan

```mermaid
flowchart LR
    Outbox[(outbox_events)] --> Worker[Outbox Dispatcher]
    Worker -->|survey.validated| ZNS[Zalo ZNS / Customer Outreach]
    Worker -->|survey.validated| Ledger[Collaborator Payroll & Billing Ledger]
    Worker -->|survey.validated| BI[BI Data Warehouse: ClickHouse / Metabase]
    Worker -->|survey.validated| Vector[DINOv2 pgvector Embeddings]
    Worker -->|survey.validated| R2[Cloudflare R2 Cold Archival]
```

### 3.1 Subsystem A: Customer Operations & Automated Outreach
Once a survey is validated (`status = 'APPROVED'`), customer-facing delivery workflows execute asynchronously without delaying the collaborator.

#### Implementation Blueprint
1. **Event Payload (`survey.validated`):**
   ```json
   {
     "event_id": "evt_9f82c401-1a3b-489e-9730-104928173491",
     "event_type": "survey.validated",
     "timestamp": "2026-11-06T10:15:30Z",
     "payload": {
       "submission_id": "sub_4091823",
       "driver_tag": "DRV-A7F92B10",
       "survey_type": "GSM_INCOME_VERIFIED",
       "customer_id": "cust_882190",
       "verified_metrics": {
         "gross_revenue": 1650000,
         "net_income": 1250000,
         "total_trips": 18,
         "vehicle_type": "CAR",
         "license_plate_masked": "29E-***.45"
       }
     }
   }
   ```
2. **Delivery Channels:**
   - **Zalo Notification Service (ZNS):** Sends an official branded template message to the driver/customer acknowledging the validated session and verified shift metrics.
   - **Customer Portal / Google Drive Clean Append:** Automatically outputs sanitized records to client delivery spreadsheets (`YYYY-MM-DD_Deliverables.xlsx`), completely stripping raw AI tokens, collaborator payout rates, and internal diagnostic flags.

### 3.2 Subsystem B: Financial Ledger, Billing & Collaborator Payroll
Automates collaborator earnings, customer billing line items, and invoice generation with atomic double-entry bookkeeping.

#### Database Schema (`schema.sql` expansion)
```sql
-- Collaborator Compensation Ledger
CREATE TABLE IF NOT EXISTS collaborator_payout_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collaborator_id VARCHAR(64) NOT NULL,
    submission_id VARCHAR(64) NOT NULL UNIQUE,
    amount_vnd INTEGER NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'VND',
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING', -- PENDING, CLEARED, PAID, CLAWBACK
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    cleared_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX idx_payout_collab_status ON collaborator_payout_ledger(collaborator_id, status);

-- Customer Invoicing & Billing Ledger
CREATE TABLE IF NOT EXISTS customer_billing_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id VARCHAR(64) NOT NULL,
    submission_id VARCHAR(64) NOT NULL UNIQUE,
    billable_unit_vnd INTEGER NOT NULL,
    tax_vat_vnd INTEGER NOT NULL,
    total_amount_vnd INTEGER NOT NULL,
    invoice_id VARCHAR(64),
    status VARCHAR(32) NOT NULL DEFAULT 'UNBILLED', -- UNBILLED, INVOICED, SETTLED
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX idx_billing_cust_status ON customer_billing_ledger(customer_id, status);
```

#### Operational Rules
- **Idempotency:** A strict unique constraint on `submission_id` ensures that even under network retries or outbox worker restarts, no collaborator or customer is ever credited or billed twice.
- **Clawback Protocol:** If a supervisor or automated audit flags fraud post-approval, the ledger issues a negative compensation entry (`status = 'CLAWBACK'`), balancing future payouts automatically.

### 3.3 Subsystem C: Reporting & BI Data Warehouse Architecture
Provides operational transparency, live fraud monitoring, and financial dashboards.

#### Key Metrics & Real-Time KPIs
1. **Latency & SLA Metrics:**
   - Synchronous loop latency (p50, p90, p99).
   - Component latency breakdown (media normalization vs AI vision vs DB commit).
2. **Quality & Compliance Yield:**
   - First-pass approval yield (target > 85%).
   - Rejection breakdown by category (`BLURRY`, `ARITHMETIC_MISMATCH`, `DUPLICATE_REUSE`, `VEHICLE_MISMATCH`).
3. **Operational Volumes:**
   - Submissions per hour by region (Hanoi, HCMC, Da Nang).
   - Vehicle fleet distribution (VinFast VF e34, VF 5, VF 8, Feliz, Klara).
4. **Fraud & Forensic Telemetry:**
   - Duplicate image reuse attempts blocked per day.
   - Screen recapture detection rates.

#### Technology Stack
- **Ingestion:** Redis Streams fan-out to ClickHouse / PostgreSQL analytics tables.
- **Visualization:** Direct read-only connection to Metabase / Looker Studio for operations leaders.

### 3.4 Subsystem D: Next-Generation Anti-Fraud Roadmap (Deep Learning Embeddings)
Upgrading from 64-bit dHash perceptual hashing to deep semantic vector embeddings.

```mermaid
flowchart TD
    RawImage[Incoming Shift Receipt / Vehicle Photo] --> Model[DINOv2 / SigLIP Feature Extractor]
    Model --> Vector["384-dimensional dense feature vector"]
    Vector --> Index["PostgreSQL pgvector HNSW Index (Cosine Distance)"]
    Index --> Match{"Cosine Similarity > 0.88?"}
    Match -- Yes --> Rej["Instant Fraud Rejection: Semantic Visual Duplicate"]
    Match -- No --> Pass["Unique Image Verified"]
```

#### Technical Plan
1. **Model:** Quantized ONNX runtime of Meta's `dinov2_vits14` (80MB model size, running on CPU in `< 45ms`).
2. **Vector Index:** PostgreSQL `pgvector` with HNSW indexing:
   ```sql
   ALTER TABLE survey_embeddings ADD COLUMN embedding vector(384);
   CREATE INDEX idx_survey_embeddings_hnsw ON survey_embeddings 
   USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
   ```
3. **Defensive Capability:** Detects cropped, color-altered, tilted, or brightness-adjusted duplicate photos that evade classical DCT/dHash hashes.

### 3.5 Subsystem E: Cold Storage Archival & Lifecycle Management
- **Hot Tier (Memory & Redis):** Temporary in-memory processing during validation (< 3 seconds).
- **Warm Tier (PostgreSQL & Local NVMe):** Master records, audit logs, and downscaled 768px WebP images (retained 90 days for operational audits).
- **Cold Tier (Cloudflare R2):** Unmodified source images uploaded in the background with zero-egress pricing. Lifecycle rules transition objects to Glacier-equivalent cold storage after 180 days, maintaining compliance with Vietnam Decree 13.

---

## 4. Capacity Scaling Milestones & Infrastructure Roadmap

| Milestone | Daily Throughput | Monthly Volume | Server Configuration | Monthly Infrastructure Cost | Primary Scaling Action |
|---|---|---|---|---|---|
| **Stage 1 (Baseline)** | 2,000 surveys/day | 60,000 | 1 Droplet (2 vCPU, 4GB RAM) | ~$24.00 / month | Single Docker Compose host (Caddy + FastAPI + PG + Redis). |
| **Stage 2 (Scale Target)** | 20,000 surveys/day | 600,000 | 1 Droplet (4 vCPU, 8GB RAM) | ~$83.00 - $93.00 / month | Worker decoupling, PgBouncer connection pooling, Redis caching. |
| **Stage 3 (Enterprise Fleet)** | 50,000+ surveys/day | 1,500,000+ | Cluster (App + Managed DB + Managed Redis) | ~$220.00 - $280.00 / month | Read-replica database scaling, multi-container horizontal worker swarm. |

---

## 5. Risk Management, SLA Guarantees & Disaster Recovery Plan

### 5.1 Primary AI Provider Outage Contingency
- **Risk:** Google AI Studio Gemini API rate limit or regional outage.
- **Contingency:** Automatic failover to DeepSeek structured vision API within a hard **4.0-second timeout boundary**. If both external providers fail, the system falls back to manual review queuing (`status = 'MANUAL_REVIEW'`), notifying the collaborator that the submission is received and queued without dropping the session.

### 5.2 Network & DNS Resilience
- **Risk:** Domestic Vietnamese ISP DNS throttling on external Google APIs.
- **Contingency:** Client-side 15s timeout with automatic fallback to Singapore edge IP geolocation (`GET /api/v1/geo/ip-fallback`), guaranteeing location acquisition within 30ms.

### 5.3 Point-In-Time Database Recovery (PITR)
- **Backup Frequency:** Automated daily PostgreSQL pg_dump exports synced to encrypted remote object storage.
- **Write-Ahead Logging (WAL):** Continuous WAL archiving allowing recovery to within 60 seconds of any hardware failure.
- **Rollback Runbook:** Clean containerized blue/green deployments enable instant version rollbacks via `git checkout <tag> && docker compose up -d --build app`.

---

*Document compiled and ratified by Principal Systems Architect. Baseline active in repository docs.*
