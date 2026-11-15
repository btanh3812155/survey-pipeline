# High-Performance Survey Verification Pipeline

Production-ready, sub-3-second automated survey verification and anti-fraud pipeline. Replaces legacy n8n visual workflows with a lightweight asynchronous Python (FastAPI) microservice, dual-model AI vision failover (DeepSeek-V4.1 Flash primary + Gemini 3.5 Flash fallback), Cloudflare R2 zero-egress storage, and PostgreSQL transactional outbox.

Designed to scale from 300 submissions/month to **20,000+ submissions/day** for under **$90/month** in total infrastructure.

---

## 1. Core Architectural Pillars

- **Sub-3s Response SLA**: Replaces 30s-2m delays with a deterministic fast-path (< 2.5s nominal, < 3.8s p99 with full AI failover).
- **Zero Double-Rotation & Alpha Black-out Bugs**: `ImageOps.exif_transpose` forces upright orientation before stripping EXIF headers; RGBA and palette `P` modes are composited over a pure white canvas.
- **Two-Tier Anti-Fraud**:
  - *Tier 1 (Synchronous, < 2ms)*: B-tree partial unique index on `(receipt_serial_no, store_tax_id) WHERE status = 'APPROVED'`.
  - *Tier 2 (Asynchronous background)*: Meta DINOv2 ONNX visual vector embeddings stored via `pgvector` HNSW index.
- **Layered Vision Failover**: Primary DeepSeek-V4.1 Flash (`deepseek-flash`, reasoning disabled) with automated circuit-breaker failover to Gemini 3.5 Flash. A deterministic layout layer (`services/income_layout.py`) locates the period tab row, reads the selected teal pill, and derives DAILY / WEEKLY / MONTHLY from the date span before compliance math runs.
- **Prompt Injection Immunity**: Extract-then-Evaluate pattern — the LLM only outputs raw JSON facts; Python deterministically evaluates compliance rules and arithmetic ($\sum \text{items} + \text{VAT} == \text{Total}$).
- **100% Financial Integrity**: PostgreSQL Transactional Outbox pattern processed via `SELECT ... FOR UPDATE SKIP LOCKED`, preventing dropped collaborator payouts during server reboots.

---

## 2. Directory Structure

```text
survey-pipeline/
├── Caddyfile                  # Automated Let's Encrypt TLS reverse proxy
├── docker-compose.yml         # Container stack: Caddy, FastAPI, PostgreSQL, Redis
├── Dockerfile                 # Python 3.12-slim production container
├── requirements.txt           # Python dependencies
├── schema.sql                 # PostgreSQL DDL (pgvector, partial indexes, outbox)
├── config.py                  # Pydantic Settings configuration
├── main.py                    # FastAPI entrypoint & webhook endpoints
├── services/
│   ├── auth_lark.py           # Lark raw SHA256 signature verification & challenge handshake
│   ├── normalizer.py          # Memory-safe WebP converter, EXIF transposition, Laplacian blur
│   ├── vision_ai.py           # Dual-model AI client with circuit breaker & mock support
│   ├── evaluator.py           # Deterministic Python compliance & arithmetic evaluation
│   ├── templates_vn.py        # Standardized Vietnamese HTML Telegram templates
│   └── storage_r2.py          # Cloudflare R2 / S3 async upload client
├── workers/
│   └── outbox_worker.py       # Asynchronous outbox processor for billing & sync
└── tests/
    └── test_pipeline.py       # 9 comprehensive unit & integration tests
```

---

## 3. Quick Start (Local Development)

### Running Automated Tests
```bash
PYTHONPATH=. uv run pytest tests/test_pipeline.py -v
```

### Running Locally with Docker Compose
1. Copy environment template:
   ```bash
   cp .env.example .env
   ```
2. Start the stack:
   ```bash
   docker compose up -d
   ```
3. Verify health:
   ```bash
   curl http://localhost:8000/health
   # {"status": "healthy", "environment": "production"}
   ```

---

## 4. Environment Variables (`.env`)

```ini
ENVIRONMENT=production
DATABASE_URL=postgresql://postgres:secret_password@postgres:5432/surveydb
REDIS_URL=redis://redis:6379/0

# Bytedance Lark / Feishu Base
LARK_APP_ID=cli_xxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxx
LARK_ENCRYPT_KEY=xxxxxxxxxxxxxxxx
LARK_VERIFICATION_TOKEN=xxxxxxxx

# Telegram Bot
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_WEBHOOK_SECRET=your_random_high_entropy_secret_string

# AI Providers
GEMINI_API_KEY=AIzaSy...
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com # or Alibaba Cloud Singapore endpoint

# Cloudflare R2 Object Storage
R2_ACCOUNT_ID=xxxxxxxxxxxxxxxx
R2_ACCESS_KEY_ID=xxxxxxxxxxxxxxxx
R2_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxx
R2_BUCKET_NAME=surveys
R2_PUBLIC_URL_PREFIX=https://cdn.yourdomain.com

# Google Drive & Administrative Account
GOOGLE_ADMIN_EMAIL=admin@trustbridgevn.com
GOOGLE_DRIVE_ROOT_FOLDER_ID=
GOOGLE_SERVICE_ACCOUNT_JSON=
```

> **Authoritative Administrative Account Notice**:
> `admin@trustbridgevn.com` is the authoritative administrative account to be used for this project, Google Drive deliverables, and all work regarding Trust Bridge. All deliverables synchronization, target shared drives, and project assets must be granted to or owned by `admin@trustbridgevn.com`.

---

## 5. Webhook Endpoints

- `POST /webhook/lark-survey`: Receives Bytedance Lark Base webhook events, verifies raw SHA256 signature, validates challenge, extracts facts via AI, and pushes instant verdict to Telegram.
- `POST /webhook/telegram`: Handles collaborator commands (`/start`) and conversational follow-up questions via Redis context cache.
- `GET /health`: Returns service health status.

---

## 6. Google Drive Synchronization (Autonomous Server-Side Architecture)

Google Drive sync is an autonomous **server-side process** running continuously in the background on the production droplet (`146.190.201.32`) inside container `survey-pipeline-app-1`.

- **No Individual Survey Uploads**: Individual survey submissions are recorded strictly into the local structured deliverables mirror (`Sheets/Master_Survey_Deliverables.csv` and `Images/{submission_id}/`) and never uploaded directly to Google Drive as loose per-survey folders.
- **Clean Drive Deliverables**: Only two synchronized deliverables exist on Google Drive:
  1. The master data sheet (`Master_Survey_Deliverables.csv`) in the root folder.
  2. The organized photo archive (`Images/` folder containing approved subfolders).
- **Autonomous Background Loop**: Managed by FastAPI lifespan (`run_periodic_drive_sync`) running on startup and repeating every 15 minutes (`interval_seconds: 900s`).
- **Deliverables Directory**: Server-side structured deliverables and Master CSV are located at `/survey-pipeline/data/google_drive_deliverables/` on the server droplet.
- **Local Workstation Policy**: **Never** run production data sync from your local machine. All sync operations and Google Drive credentials reside on the production server.

### Manual Sync Execution

#### Method 1: Via HTTP API (No SSH Required)
Trigger an immediate background synchronization via authenticated curl:
```bash
curl -X POST https://survey.trustbridgevn.com/api/v1/admin/sync-drive \
     -u admin:<ADMIN_PASSWORD>
```

#### Method 2: Via Server Terminal (Remote SSH)
Run the standalone sync script inside the production Docker container:
```bash
ssh root@146.190.201.32 "docker exec -it survey-pipeline-app-1 python scripts/sync_to_drive.py"
```

To perform a dry-run preview (checks local deliverables without pushing files to Drive):
```bash
ssh root@146.190.201.32 "docker exec -it survey-pipeline-app-1 python scripts/sync_to_drive.py --dry-run"
```

#### Method 3: Local Development
```bash
python3 scripts/sync_to_drive.py [--dry-run]
```
