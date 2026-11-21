# Master Internal System Specification: Survey Verification Pipeline

**Document Version:** 1.4.0 (historical product/design requirements; not a verified production-release description)
**System Target:** 2,000 to 20,000+ submissions/day (~600,000/month)  
**Nominal Latency SLA:** < 2,500 ms (p50), < 3,800 ms (p99 under complete AI failover)  
**Target Operating Budget:** ~$83.00 - $93.00 / month (at 20k/day scale)
**Authoritative Administrative Account:** `admin@trustbridgevn.com` (Designated for Google Drive deliverables and all Trust Bridge operations)

> **Upgrade authority:** Use [MODULE_DESIGNER_HANDOFF.md](MODULE_DESIGNER_HANDOFF.md) for the observed baseline and [MODULE_IMPLEMENTATION_PLANS.md](MODULE_IMPLEMENTATION_PLANS.md), especially sections 1.6-1.8, for this migration's dispositions and tests. Claims below about completed behavior, quotas, infrastructure, privacy commands, performance or legal compliance are historical requirements/claims, not proof or permission to override the current source and explicit approval gates. In particular, this upgrade preserves the plan's per-channel quota table rather than silently adopting 3 appeals/8h.

---

## 1. Executive Summary & Architecture Principles

The pipeline is an automated, real-time survey verification, anti-fraud, and financial accounting engine built for mobile vehicle inspections (specifically GSM / Xanh SM VinFast bike and car fleets in Vietnam).

### Core Principles
1. **Boring, Clean, Single-Language Core**: Completely built in Python (FastAPI + Uvicorn) inside Docker. Eliminates n8n memory bloat, base64 node-to-node overhead, and fragmented failure domains.
2. **Liberal Rejection Policy**: Because drivers are active in the field and retaking a photo takes 10 seconds, the system rejects any doubtful image or arithmetic mismatch immediately. Supervisors are never spammed with hundreds of soft-flag tickets.
3. **Strict Human Review Escape Hatch**: Rejections include a 1-click appeal button (`Yêu cầu kiểm tra lại`), strictly rate-limited to **3 requests per 8-hour window** via Redis.
4. **Extract-then-Evaluate Isolation**: The AI vision models are restricted strictly to passive factual transcription (zero math, zero decision logic). Deterministic Python code evaluates all compliance and arithmetic rules, rendering prompt injection impossible.

---

## 2. Infrastructure, Cloud & Network Topology

### 2.1 Geographic Placement & Cloud Provider
- **Primary Recommendation:** DigitalOcean SGP1 (Singapore).
  - Direct subsea fiber corridors to Vietnam (Viettel, VNPT, FPT) provide **20-30 ms ping** to Saigon and Hanoi.
  - SGP1 maintains single-digit millisecond peering to Bytedance Lark's regional cluster, Google's Singapore edge, and Telegram core datacenters.
  - Sizing: Basic Droplet (2 vCPU, 4GB RAM, 80GB NVMe) easily handles 20k submissions/day for $24/mo.
- **Cloud Agnosticism:** The entire stack is containerized via standard `docker-compose.yml` (Caddy, FastAPI, PostgreSQL, Redis). It can be deployed identically on Hetzner Singapore, AWS (`ap-southeast-1`), or on-premise hardware without code changes.

### 2.2 DigitalOcean Role-Based Access Control (RBAC) & Team Security
- **Does DigitalOcean support RBAC? Yes.** DigitalOcean supports multi-user **Teams**, **Custom Roles**, and **Project-based IAM**:
  1. **Built-in Organization Roles:**
     - `Owner`: Full administrative, member, and billing control.
     - `Member`: Can manage droplets, databases, and network resources within assigned projects.
     - `Biller`: View invoices, manage payment cards, download tax receipts. **Strictly locked out from servers, databases, and code.**
  2. **Project-Level Isolation:** Resources are partitioned into Projects (`Production-GSM`, `Staging`). Developers and contractors can be scoped strictly to a specific project.
  3. **Custom Scoped Roles:** Permissions can be assigned per resource category (e.g. `Droplet Operator`, `Database Administrator`, `Read-Only Viewer`).
  4. **Scoped API Tokens:** Personal Access Tokens (PATs) can be restricted to specific permissions (e.g. read-only metrics vs full resource write).
  - *Comparison with AWS:* DigitalOcean RBAC is not as infinitely granular as AWS IAM (which has thousands of individual JSON condition blocks), but it is clean, team-focused, and completely prevents junior devs from accessing billing or deleting production clusters without the nightmare complexity of AWS IAM policy debugging.

### 2.3 Ingress & Security Layer (Caddy)
- Caddy automatically terminates TLS 1.3 using free Let's Encrypt certificates.
- Enforces a hard **25MB request body cap** to prevent memory exhaustion attacks.
- Reverse proxies to 4 Uvicorn workers with HTTP keep-alive connection pooling.

### 2.4 Webhook Cryptographic Verification
- **Lark Base Webhooks:** Lark does **not** use HMAC. Verification hashes raw byte concatenation:
  $$\text{Signature} = \text{hex}(\text{sha256}(\text{timestamp} + \text{nonce} + \text{encrypt\_key} + \text{raw\_body}))$$
  Evaluated in constant time using `secrets.compare_digest`.
- **URL Challenge Handshake:** When registering webhooks, Lark sends `{"type": "url_verification", "challenge": "..."}`. The gateway echoes the challenge token in < 1s (decrypting via AES-256-CBC if encryption is enabled).
- **Telegram Webhook:** Validates `X-Telegram-Bot-Api-Secret-Token` via `secrets.compare_digest`.
- **Deduplication:** Redis executes `SETNX lock:submission:{id} 1 EX 60`. Duplicate webhook retries are dropped with an immediate `200 OK`.

### 2.5 Company Domain Access & DNS Routing Architecture
To connect the survey verification pipeline to the company's domain (e.g., `survey.company.com` or `api-survey.company.com`):
1. **Subdomain A-Record Delegation:**
   - In the company's DNS management panel (Cloudflare, GoDaddy, Namecheap, Route53, or Vietnamese registrar):
     - **Record Type:** `A`
     - **Name / Host:** `survey` (or `api-survey`)
     - **IPv4 Address:** `<DROPLET_RESERVED_IP>` (e.g. `128.199.xxx.xxx`)
     - **TTL:** `300` seconds (5 minutes for agile cutover).
     - *(Optional Dual-Stack)*: Add an `AAAA` record pointing to the Droplet's public IPv6 address.
2. **Zero-Touch Automatic TLS via Caddy:**
   - In `Caddyfile`, configure the domain:
     ```caddyfile
     survey.company.com {
         encode zstd gzip
         reverse_proxy fast-validator:8000
     }
     ```
   - Caddy automatically provisions and auto-renews free Let's Encrypt / ZeroSSL TLS certificates with zero manual certbot intervention.
3. **Cloudflare Proxy / Zero-Trust Option (No Open Ports):**
   - If using Cloudflare proxy (orange cloud), Caddy can terminate Cloudflare Origin CA certificates, or the team can spin up `cloudflared` tunnel in docker-compose, allowing the server to accept traffic with ports 80/443 closed at the droplet firewall.

4. **Battlefield Playbook: When the Client Lacks a DNS Manager or Technical Staff:**
   - **Step 1: 5-Second DNS Detective (Find who holds the keys):**
     Run `dig NS company.com +short` or `whois company.com`.
     - If nameservers end in `matbao.vn` -> Domain is hosted at Mắt Bão (`id.matbao.net`).
     - If nameservers end in `pavietnam.vn` -> Domain is hosted at P.A Việt Nam (`support.pavietnam.vn`).
     - If nameservers end in `domaincontrol.com` -> Domain is on GoDaddy.
     - If nameservers end in `cloudflare.com` -> Domain is on Cloudflare.
     Tell the client: *"Tên miền công ty mình đang đặt tại [Nhà cung cấp]. Anh/chị nhờ kế toán tìm email hóa đơn gia hạn thường niên hoặc nhờ bạn kỹ thuật website thêm 1 bản ghi A là xong ạ."*
   - **Step 2: Agency Delegation Template (Zero Client Friction):**
     If an external web design agency manages their WordPress/hosting, send them this snippet:
     > *"Em chào anh/chị kỹ thuật, bên em đang tích hợp cổng khảo sát cho công ty. Nhờ anh/chị tạo giúp 1 subdomain `survey.company.com` trỏ bản ghi A về IP: `<DROPLET_IP>`, TTL 300s. Em cảm ơn ạ!"*
   - **Step 3: Zero-DNS Instant HTTPS Workaround (`sslip.io` / `nip.io`):**
     Both WebRTC camera access (`getUserMedia`) and Telegram Webhooks strictly require trusted SSL/TLS (HTTPS). If the client cannot access DNS immediately:
     - Point Caddy to `<DROPLET_IP>.sslip.io` (e.g. `128.199.50.84.sslip.io`).
     - `sslip.io` automatically resolves to the droplet IP globally with zero DNS configuration.
     - Caddy automatically obtains a trusted Let's Encrypt SSL certificate for it in 15 seconds.
     - The entire WebRTC camera form, API webhooks, and Telegram bot can run immediately without waiting for company DNS approval.
   - **Step 4: Dedicated Companion Domain ($1.50 - $10 on Company Card):**
     If the corporate domain is tied in bureaucracy or has fragile email configurations (MX/SPF/DKIM), buy a dedicated companion domain (e.g. `company-survey.com` or `company-survey.vn`) directly on Cloudflare Registrar or Namecheap using the company credit card. This grants the pipeline 100% operational autonomy with zero risk to the corporate website.

---

## 3. Media Sanitization & Image Scaling Specification

### 3.1 Scaling Behavior: 720px Short Edge & 2160px Long Edge Dual-Bound
- **Specification:** The pipeline applies a **dual-bound aspect-ratio preserving downscale**:
  - **Maximum Short Edge:** **720px**
  - **Maximum Long Edge:** **2160px**
  - **Maximum Aspect Ratio:** **6.0 : 1** (safeguards against absurd ribbon/strip files like 50x5000).
- **Mathematical Formulation:**
  $$\text{scale} = \min\left(1.0, \frac{2160}{\max(w, h)}, \frac{720}{\min(w, h)}\right)$$
  $$\text{new\_w} = w \times \text{scale}, \quad \text{new\_h} = h \times \text{scale}$$
- **Behavior across Device Formats:**
  - Standard 4:3 camera photo ($4000 \times 3000$): Short edge clamps to 720 -> Result: **$960 \times 720$** (crisp, zero distortion).
  - Modern tall smartphone screen ($1080 \times 2400$, 20:9): Short edge clamps to 720 -> Result: **$720 \times 1600$** (tall UI preserved!).
  - Long multi-item receipt ($1000 \times 4000$, 4:1): Long edge clamps to 2160 -> Result: **$540 \times 2160$** (all items readable!).
  - Lark square crop ($3072 \times 3072$): Clamps to **$720 \times 720$**.
  - Extreme corrupted ribbon ($50 \times 1000$, 20:1 > 6:1): Safely rejected with descriptive error.
### 3.2 Bug Eliminators
1. **Memory Spike Elimination:** High-res smartphone captures (48MP - 50MP) can allocate ~196MB of uncompressed raster RAM. Applying libjpeg-turbo C draft scaling (`im.draft('RGB', (1024, 1024))`) decodes at 1/4 scale in C, slashing memory allocation by **85%** (~12MB peak).
2. **Double-Rotation Elimination:** `ImageOps.exif_transpose` rotates the pixel matrix upright, and EXIF orientation headers are explicitly wiped (`exif=b""`) when saving WebP. Downstream clients never apply a second rotation.
3. **Alpha Blackout Elimination:** All alpha-bearing modes (`RGBA`, `LA`, and palette `P` mode with `transparency` metadata) are composited over a pure `#FFFFFF` white canvas. Black receipt text never collapses into black backgrounds.
4. **Compression:** Encoded to WebP (quality 82), reducing average file size from 2.2MB down to **49.4KB** (93.9% bandwidth reduction).

### 3.3 Custom WebRTC Mobile Camera Form (Anti-Gallery Upload)
- **The Lark Base Mobile Exploit:** Lark Base open web forms use standard `<input type="file">`. On mobile browsers (Safari/Chrome), this prompts drivers: *"Take Photo or Choose from Photo Library"*. Fraudulent drivers exploit this to upload pre-saved photos, images shared by other drivers, or edited screenshots from their camera roll.
- **The Solution: In-House WebRTC Live Camera App (`/camera-form`):**
  1. **Strict Hardware Stream Enforcement:**
     - Invokes `navigator.mediaDevices.getUserMedia({ video: { facingMode: { exact: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } } })`.
     - Completely bypasses the mobile OS file picker and gallery selection dialog.
  2. **Real-Time On-Screen Viewfinder:**
     - Renders the live video stream directly on canvas with alignment guides for receipts, vehicle plates, and odometer screens.
     - Captures snapshots directly from the hardware video stream buffer (`ctx.drawImage(video, 0, 0)`), guaranteeing that every pixel originates from the live physical camera sensor.
  3. **Client-Side Integrity Watermark & Fast Upload:**
     - Watermarks frame with client timestamp and accelerometer stability metrics.
     - Encodes directly to lightweight WebP (quality 80) and streams directly to `/api/v1/survey/submit-webrtc`.

---

## 4. Generalized Physical Quality & Pre-Filters

### 4.1 Chronological Order: Forensic Scan First
The pipeline explicitly scans raw uncompressed bytes for forensics **before** auto-rotation or metadata purging:
1. **EXIF Tag 305 Scan:** Checks for banned editing software signatures (`photoshop`, `canva`, `picsart`, `snapseed`, `lightroom`, `meitu`).
2. **Error Level Analysis (ELA):** Resaves at 95% JPEG quality and measures localized pixel difference variance. Splicing or digital eraser marks create compression boundary spikes (`ela_max > 85` with low background mean).

### 4.2 Generalized Quality Pre-Filters
1. **Blur Gate:** OpenCV Laplacian variance (`cv2.Laplacian(gray, cv2.CV_64F).var()`). Variance < 100.0 rejects shaky photos in ~25ms.
2. **Digital Screenshot Exemption:** `is_digital_screenshot()` detects flat UI blocks and zero camera noise. Digital screenshots (whether white theme or AMOLED dark mode) are **100% exempt from exposure checks**.
3. **AMOLED-Safe Exposure Check:** For physical camera photos, checks the **95th percentile peak dynamic range (`p95`)**, not mean luminance:
   - Real pitch-black night photo: `p95 < 35` -> Rejects as `IMAGE_TOO_DARK`.
   - AMOLED dark mode screenshot: `p95 == 255` -> 100% safe!
4. **Specular Glare Gate:** For camera photos, checks if a specular hotspot obliterates local edge gradients (Sobel edge density < 15 inside saturated zone). White paper with black text is 100% safe.

---

## 5. Fleet Consistency, Anti-Fraud & Income Sanity

### 5.1 Persistent Vehicle Tracking & Admin Vehicle Changes
- `driver_registered_vehicles` table permanently binds `driver_phone` to `license_plate` and `vehicle_type` (`BIKE` vs `CAR`).
- If an incoming survey has a different plate or vehicle type, it is rejected as `VEHICLE_UNREGISTERED`.
- **Admin Vehicle Change Pathway:**
  - Legitimate vehicle changes (company replacements, rental upgrades) are approved via the moderation group button: `[ 🔄 Cho phép đổi xe ]`.
  - Updates active plate in `driver_registered_vehicles` and logs entry to `vehicle_change_history`.
  - **Audit & Frequency Monitoring:** If a driver changes vehicles $\ge 3$ times within 30 days, the bot alerts supervisors with a high-frequency warning badge.

### 5.2 Online Stock Photos & AI Diffusion Detection
- **dHash Stock Match:** 64-bit difference hash compared against `online_stock_images` reference database. Distance $\le 6$ rejects as `STOCK_PHOTO_DETECTED`.
- **2D FFT Spectral Analysis:** Detects periodic high-frequency checkerboard grid artifacts characteristic of diffusion models (Midjourney, DALL-E, Flux, Stable Diffusion). Ratio > 1.35 rejects as `AI_GENERATED_DETECTED`.

### 5.3 Income Sanity & Micro-Ride Accounting
- **Hard Ceiling Auto-Reject:** Monthly revenue exceeding operational fleet bounds (e.g. bike driver claiming > 45M VND or $10k USD) is rejected as `INCOME_EXCEEDS_THRESHOLD`.
- **Micro-Ride Check:** Verifies average fare per ride ($\frac{\text{gross}}{\text{trips}}$). Out-of-range averages reject as `AVERAGE_FARE_OUT_OF_RANGE`.
- **Accounting Reconciliation:** Verifies $\text{Gross} - \text{Discount} - \text{Tax} == \text{Net}$. Discrepancies > 5% reject as `ARITHMETIC_MISMATCH`.


### 5.4 Driver Pseudonymization & Identity Vault (`driver_id` Tokenization)
- **Least Privilege & Decree 13 Compliance:** Raw driver phone numbers are classified as sensitive PII under Vietnamese law. They must NEVER be passed across internal microservice hops, redis queues, or analytics workers.
- **The Identity Vault (`driver_identity_vault`):**
  - Upon first encounter, the driver's phone number is securely bound to an opaque token: `driver_id` (e.g. `DRV-8A2F9`).
  - All subsequent database queries, Redis rate-limiting keys, AI vision prompts, and Outbox event payloads refer strictly to `driver_id`.
  - The raw phone number is stored in an encrypted/isolated vault and is only accessed by the final Zalo ZNS worker at the very edge of delivery.
---

## 6. Dual-Model AI Vision Engine

### 6.1 Provider Hierarchy & Latency Budget
- **Primary:** DeepSeek Flash (via Alibaba Cloud Singapore `ap-southeast-1` or official API). Per-call hard timeout: **2.0s**.
- **Backup:** Gemini 3.5 Flash (**strictly no reasoning / thinking disabled** via `extra_body={"thinking_config": {"thinking_budget": 0}}`).
- **Circuit Breaker:** 5 consecutive primary timeouts or 5xx errors trips the breaker to `OPEN` for 60 seconds, routing directly to Gemini.

### 6.2 Prompt-Level Anti-Spoofing
- Gemini and DeepSeek are instructed to detect physical screen recaptures (moiré pixel grids, glare, monitor bezels), setting `is_screen_photo = True` -> rejected as `DEVICE_SCREEN_DETECTED`.

---

## 7. Telegram Operational Architecture & Group Privacy

### 7.1 Driver Isolation & Mod Live Relay
- Telegram does not support selective per-member message hiding in a single group.
- **The Implemented Architecture:**
  1. Drivers interact **strictly in 1-to-1 private chat with the Bot** (`t.me/TenBot`). Drivers physically see **only their own messages, photos, and payouts**.
  2. The bot automatically relays **100% of driver messages, photos, and bot replies into the Telegram Management / Admin Group(s)** in real time.
  3. Admins observe all fleet activity live in their group.

### 7.2 In-Group Admin Moderation Controls
Under any forwarded survey or appeal ticket in the management group, the bot attaches 1-click inline buttons:
- `[ ✅ Duyệt ]`: Overrides rejection, patches status to `APPROVED`, credits payout, and syncs client deliverables.
- `[ ❌ Từ chối ]`: Confirms final rejection with notification to driver.
- `[ 🔄 Cho phép đổi xe ]`: Approves vehicle change and logs audit history.
- `[ ⚠️ Cảnh cáo ]`: Issues formal compliance warning.

### 7.3 Rate-Limited Appeal Quota (Max 3 / 8 Hours)
- Rejected drivers see the inline button: `[ 🙋 Yêu cầu kiểm tra lại ]`.
- Redis counter (`rate_limit:appeal:{driver_phone}`) enforces **maximum 3 appeals per 8-hour window**.
- Rejection messages inform drivers of remaining quota: `(Bạn còn X/3 lượt yêu cầu kiểm tra lại trong 8 giờ này)`.

### 7.4 Pre-Determined Natural Language Message Mapping
No raw technical codes (e.g. `INCOME_EXCEEDS_THRESHOLD`) are shown to drivers. Every code maps to standardized natural Vietnamese sentences:
- Blurry: *"Ảnh chụp bị rung tay hoặc lóa đèn, bạn vui lòng giữ chắc tay và chụp lại nhé!"*
- Mismatch: *"Số liệu doanh thu không khớp với chi tiết các khoản, bạn vui lòng đối soát lại nhé!"*
- Vehicle: *"Phương tiện không trùng khớp xe đã đăng ký. Nếu bạn đổi xe mới, vui lòng bấm nút bên dưới để quản trị viên hỗ trợ nhé!"*


### 7.6 Media Management & Form Description Images: Caddy vs. Cloudflare R2 Long-Term Upgrade

#### 1. Why Google Drive is Explicitly Prohibited for Public Guide Images
- **Latency Degradation:** Google Drive links enforce a 302 redirect cascade through `doc-xx.googleusercontent.com` and cookie checks, adding **800ms - 2,500ms TTFB** per image on mobile 4G.
- **Rate Limiting (403 Quota Exceeded):** Drive enforces per-file concurrent access limits (~100-200 views/hour), causing broken images and virus-scan warning interstitials during peak shift changes.
- **Zero Edge Caching:** Drive sets `cache-control: private, max-age=0`, forcing mobile browsers to re-download images repeatedly over cellular data.

#### 2. Short-Term / Active Deployment: Caddy Static Edge Mount (`/media/*`)
- **Storage Path:** `/root/survey-pipeline/data/media/{guide,forms,general}/`
- **Serving Layer:** Caddy reverse-proxies directly, compresses via Gzip/Zstandard, and serves images with aggressive browser caching headers (`Cache-Control: public, max-age=31536000, immutable`).
- **Latency:** **< 15 ms** TTFB.
- **Management UI:** Built-in Google Drive-style Media Manager at `/admin/media` with drag-and-drop WebP normalization and 1-click Markdown copying (`![alt](url)`).

#### 3. Long-Term Upgrade: Cloudflare R2 + CDN Edge (Zero Egress)
- **Architecture:** The codebase is pre-wired for Cloudflare R2 via `R2StorageClient` (`services/storage_r2.py`).
- **Pricing Advantages:**
  - Storage: $0.015 / GB / month (first 10 GB free).
  - Read Operations: Class B free up to 10,000,000 requests/month.
  - **Egress Bandwidth: $0.00 (Completely free forever, eliminating standard AWS S3 egress bill shock).**
- **Edge Acceleration:** Binding R2 to a custom domain (e.g. `assets.trustbridgevn.com` or `cdn.trustbridgevn.com`) leverages Cloudflare's 300+ global edge points of presence (including VNPT and Viettel nodes in Hanoi and Ho Chi Minh City) for sub-20ms mobile rendering.
- **Zero-Code-Change Cutover:** When `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` are supplied in `.env`, `R2StorageClient` automatically routes uploads to R2 and generates public CDN URLs. The Media Manager frontend and form rendering code require zero modification.

#### 4. Separation of Submission Images vs. Description Guide Images
- **Form Submission Images (Driver Uploads):** Handled via `services/storage_r2.py` (uploads to Cloudflare R2 bucket `surveys` or local fallback `./data/uploads/`) and mirrored into `./data/google_drive_deliverables/Images/{submission_id}/` for client verification spreadsheets.
- **Form Description Images (Admin Guide Photos):** Managed via `/admin/media`, stored under `./data/media/` and served through Caddy at `/media/*` (or Cloudflare R2 CDN prefix).
### 7.5 Driver & Collaborator Role Separation
- **Role RBAC:** `authorized_users` explicitly distinguishes `DRIVER` vs `COLLABORATOR`:
  - **COLLABORATOR:** Fleet leaders and agency recruiters submitting surveys on behalf of drivers or monitoring regional teams; receives aggregated payout ledgers.
  - **DRIVER:** Individual vehicle operators; submits vehicle data, checks survey status, appeals rejections, or requests personal data erasure.
- Both roles interact through the Telegram bot with role-specific views and permission boundaries.

### 7.6 Internal Telegram Conversation Logging & Data Scrubbing (Decree 13 / GDPR)
- **Internal Message Persistence:** All bot conversations are logged to `telegram_chat_history` (rather than relying on Telegram-level ephemeral message settings).
- **Right to Erasure (`/scrub_my_data`):**
  - Drivers can trigger automated personal data erasure.
  - The system sets `driver_phone = NULL`, `driver_name = '[SCRUBBED]'` across `driver_identity_vault` and `survey_submissions`, and anonymizes chat logs.
  - Emits an immutable audit record in `system_audit_log` proving compliance with Decree 13 Article 16.

### 7.7 Automated 36-Hour Ephemeral Message Deletion Queue
- Ephemeral messages sent to drivers are tracked in `telegram_sent_messages` with `delete_after = NOW() + INTERVAL '36 hours'`.
- A background cleanup task invokes Telegram API `deleteMessage(chat_id, message_id)` once 36 hours elapse, ensuring zero residual sensitive chat history on mobile devices.

### 7.8 Sanitized Rejection Image Dispatching
- When a submission is rejected for physical flaws (blur, glare, framing), an illustrative crop/image is sent with the rejection notice.
- **Sanitization Requirement:** Before transmission to Telegram, the image is passed through `sanitize_rejection_image_and_metadata()` which strips all EXIF/GPS tags and blurs any sensitive facial or license plate regions.
---

## 8. Data Persistence, Outbox & Downstream Outputs

### 8.1 PostgreSQL Master Schema & Outbox Pattern
- Masters table `survey_submissions` and `outbox_events` are written in a single atomic transaction.
- **Conditional Partial Unique Index:**
  ```sql
  CREATE UNIQUE INDEX idx_surveys_serial_tax_approved 
  ON survey_submissions (receipt_serial_no, store_tax_id) 
  WHERE status = 'APPROVED';
  ```
  Permits immediate sharp retakes of previously blurry rejected photos.
- Outbox worker polls using `SELECT ... FOR UPDATE SKIP LOCKED` (bypassing PgBouncer session pooling limits).

### 8.2 The Two Downstream Outputs
1. **Management Operations Dashboard (Lark Base on Company PCs):**
   - Admin Master Grid (raw audit trail, driver phones, export to Excel).
   - QC Supervisor Kanban (Approved / Rejected / Appeals with 1-click overrides).
   - Telesales Workspace (approved leads, verified phones, call status dropdown).
2. **Google Drive Customer Deliverables Folder:**
   - Outbox worker copies approved WebP photos and appends rows to a clean shared Google Sheet (`YYYY-MM-DD/Client_Report.xlsx`). Internal diagnostic flags, raw AI outputs, and collaborator payout rates are stripped.

### 8.3 Zalo ZNS Gateway (No Consent Text)
- Sends automated official thank-you to driver phone number via Zalo Notification Service.
- Legal consent is captured in the Lark form once and for all; ZNS message focuses purely on thank-you and connection for future surveys.


### 8.4 Granular Hop-by-Hop Audit Logging (`system_audit_log` Hop Chain)
Every single piece of data is audited across 8 deterministic sequential hops with cryptographic SHA-256 block hashing ($H_n = \text{SHA256}(H_{n-1} + \text{ts} + \text{action} + \text{details})$):
1. **`HOP_1_INGEST`:** Webhook / WebRTC payload reception & signature validation.
2. **`HOP_2_NORMALIZER`:** Dual-bound scaling, EXIF auto-rotation, RGB flattening, Laplacian blur check.
3. **`HOP_3_ANTI_FRAUD`:** pHash 64-bit Hamming index search & DINOv2 vector similarity lookup.
4. **`HOP_4_AI_VISION`:** DeepSeek / Gemini Flash multimodal fact extraction.
5. **`HOP_5_EVALUATOR`:** Business rule validation, license plate check, income sanity check.
6. **`HOP_6_TG_NOTIFY`:** Notification dispatch to collaborator/driver Telegram chat & 36h queue.
7. **`HOP_7_OUTBOX`:** Atomic commit of master record and decoupled transactional outbox events.
8. **`HOP_8_DOWNSTREAM`:** Independent execution by per-flow workers (Billing, R2, GDrive, Lark, ZNS).

### 8.5 Zero-PII Rejected Submission Persistence
- Rejected submissions are permanently recorded in `survey_submissions` for fleet auditability.
- **Strict PII Stripping:**
  - `status = 'REJECTED'`
  - `driver_phone = NULL` (bound only to pseudonymized `driver_id`).
  - `image_r2_url = NULL` and `signature_r2_url = NULL` (unapproved raw photos are NEVER persisted to cloud storage).
  - Retains only rejection reason code, pHash bitmask, forensic scores, and tamper-evident SHA-256 seal.
---

## 9. Verification & Empirical Benchmark Summary

- **Automated Test Suite:** 18 unit & integration tests passing 100% green in 2.06 seconds.
- **Empirical GSM Dataset (648 VinFast photos):**
  - Raw 2.2MB photos compressed to **49.4KB WebP (93.9% bandwidth savings)**.
  - 102 blurry photos (15.7%) caught in ~25ms.
  - 2 photoshopped files caught by ELA forensics.
  - 100% precision on screen recapture spoofing.
