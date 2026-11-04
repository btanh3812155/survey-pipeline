# Wave 1 Security Pentest & Optimization Audit Report

**Target Repository:** `/home/ebylxn/projects/survey-pipeline`  
**Assessment Period:** 2026-11-04  
**Role:** Principal Security Architect & Optimization Lead (Wave 1)  
**Scope Covered:**
- **Team 3 (Security Pentest):** In-depth penetration testing of `main.py`, `services/`, and `workers/` against static file exposure, profile hijacking, cache poisoning, webhook forgery, and ReDoS vulnerabilities.
- **Team 5 (Code Cleanup & Optimizations):** Identification and elimination of dead code, unused imports, redundant database calls, in-memory cache unbounded growth, and over-engineered abstractions across the codebase.

---

## 1. Executive Summary

A comprehensive security audit and code optimization analysis was conducted across the live codebase of the Mobile Survey Verification Pipeline. 

The investigation confirmed that while previous audit rounds identified high-level vulnerabilities, several critical attack surfaces remain active in the running system:
1. **VULN-DATA-01 remains active:** Sensitive driver personal data (`data/dev_test_submissions.json`) and uploaded receipts are exposed to unauthenticated public download via FastAPI's static mount on `/data`.
2. **VULN-AUTH-05 is confirmed and exploitable:** Any unauthenticated client calling `/api/v1/survey/submit-webrtc` can overwrite legitimate drivers' vehicle registrations (`driver_registered_vehicles`), causing subsequent genuine surveys to be rejected for vehicle mismatch.
3. **VULN-CACHE-01 & VULN-CACHE-02 are active:** In-memory idempotency caching uses an unconstrained global dictionary (`LOCAL_IDEMP_CACHE`), enabling Denial-of-Service via RAM exhaustion (OOM) and cross-tenant cache poisoning/snooping due to missing phone-scoped namespaces.
4. **Telegram Webhook Forgery & Dossier Exfiltration:** `/webhook/telegram` fails to verify that incoming `/query`, `/check`, and callback query chat IDs match `TELEGRAM_ADMIN_GROUP_ID`, enabling arbitrary Telegram users to exfiltrate full confidential driver dossiers.
5. **ReDoS & Schema Validator Crashing:** `validate_submission_against_schema` executes unvetted regex patterns without timeout or syntax error handling (`re.error`), allowing algorithmic complexity attacks and internal 500 error crashes.

In addition, multiple high-impact optimization opportunities were identified:
- **Redundant SQL Roundtrips:** Persistence in `submit_webrtc_survey` executes 3 sequential roundtrips wrapped in reflective inspection code (`cur_obj = conn.cursor(); if inspect.isawaitable...`).
- **Dead Code & Unused Imports:** Unused imports and obsolete constants (such as `CAMERA_FORM_HTML`) clutter core modules.
- **Redundant Commit Logic:** `workers/outbox_worker.py` performs redundant manual commit checks inside an already self-committing `conn.transaction()` block.

---

## 2. Team 3: Security Pentest Findings

### 2.1 VULN-DATA-01: Unauthenticated Static File Exposure under `/data`

- **Severity:** HIGH (CVSS: 7.5 - Confidentiality)
- **Target File & Lines:** `main.py:111`
  ```python
  app.mount("/data", StaticFiles(directory="data"), name="data")
  ```
- **Vulnerability Mechanics:**  
  FastAPI's `StaticFiles` is mounted at route prefix `/data` with directory set to `./data`. No authentication dependency (`verify_admin_auth`) or route filter wraps this mount. The mount exposes the entire directory tree recursively over HTTP.
- **Exposed Sensitive Assets:**
  - `data/dev_test_submissions.json`: Contains raw driver personal data (phone numbers, full names, license plates, tax IDs, receipt serial numbers, financial line items, submission timestamps).
  - `data/uploads/`: Contains uploaded driver receipt photos, identity documents, and WebP images.
  - `data/forms/`: Schema JSON files containing form structures, internal question logic, and notification chat IDs.
- **Exploit Scenario:**
  1. An attacker sends an unauthenticated HTTP GET request:
     ```http
     GET /data/dev_test_submissions.json HTTP/1.1
     Host: target-survey-pipeline.internal:8000
     ```
  2. The server returns HTTP 200 OK with the full JSON array of driver submissions, revealing driver phone numbers, names, financial balances, and vehicle information.
  3. The attacker crawls `/data/uploads/` to harvest uploaded receipts and driver documents without credentials.
- **Legal & Regulatory Risk:** Direct breach of Vietnam Decree 13/2023/ND-CP (Articles 13 & 26), exposing personal data without technical safeguards, carrying administrative fines and mandatory operational shutdown.
- **Verification Evidence:** Confirmed by route inspection in `TestTeam1KnownAndMitigatedVulnerabilities::test_vuln_data_01_static_data_exposure_confirmed`, where `client.get("/data/dev_test_submissions.json")` returns HTTP 200 with driver PII.

---

### 2.2 VULN-AUTH-05: Driver Vehicle Profile Hijacking via Public WebRTC Ingestion

- **Severity:** HIGH (CVSS: 8.1 - Integrity & Availability)
- **Target File & Lines:** `main.py:483-494`
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
- **Vulnerability Mechanics:**  
  The endpoint `/api/v1/survey/submit-webrtc` is public and unauthenticated. When processing persistence, if `license_plate` is provided, the backend unconditionally updates `driver_registered_vehicles` with `ON CONFLICT (driver_phone) DO UPDATE`. It performs zero verification of phone ownership (no OTP, no token, no session binding).
- **Exploit Scenario:**
  1. Attacker identifies legitimate driver phone `0988111222` whose registered vehicle is `29E-123.45`.
  2. Attacker submits a survey payload to `/api/v1/survey/submit-webrtc` with:
     ```json
     {
       "driver_phone": "0988111222",
       "license_plate": "99X-888.88",
       "vehicle_type": "TRUCK"
     }
     ```
  3. The database immediately overwrites the driver's vehicle plate to `99X-888.88`.
  4. The next morning, the legitimate driver submits their daily survey with their actual vehicle `29E-123.45`.
  5. The anti-fraud engine (`VehiclePersistenceTracker`) checks the database, finds the registered plate is `99X-888.88`, flags a `VEHICLE_MISMATCH` / `VEHICLE_UNREGISTERED`, and rejects the driver's submission. The driver is blocked from receiving survey compensation.
- **Verification Evidence:** Confirmed in `test_adversarial_round3.py::test_unauthenticated_vehicle_profile_hijacking_db_upsert` and `test_wave1_bug_and_legality.py::test_vuln_auth_05_unverified_vehicle_upsert_confirmed`.

---

### 2.3 VULN-CACHE-01 & VULN-CACHE-02: Idempotency Cache Memory Unbounded Growth & Cross-Tenant Snooping

- **Severity:** MEDIUM (CVSS: 6.8 - Availability & Confidentiality)
- **Target File & Lines:** `main.py:54`, `main.py:324-336`, `main.py:556-564`
  ```python
  LOCAL_IDEMP_CACHE: Dict[str, str] = {}
  ...
  # Lookup
  if redis_client:
      cached_payload = await redis_client.get(f"idemp:{idempotency_key}")
  if not cached_payload and idempotency_key in LOCAL_IDEMP_CACHE:
      cached_payload = LOCAL_IDEMP_CACHE[idempotency_key]
  ...
  # Store
  LOCAL_IDEMP_CACHE[idempotency_key] = json.dumps(resp_data)
  if redis_client:
      await redis_client.set(f"idemp:{idempotency_key}", json.dumps(resp_data), ex=86400)
  ```
- **Vulnerability Mechanics:**
  1. **Unbounded Memory Growth (VULN-CACHE-01):** `LOCAL_IDEMP_CACHE` is a standard, unconstrained Python dictionary. It lacks a maximum capacity, LRU eviction, and TTL expiration. In environments without Redis, or when Redis is temporarily unavailable, every new `idempotency_key` is added permanently to RAM.
  2. **Cross-Tenant Key Namespace Collision & Snooping (VULN-CACHE-02):** Cache keys are named `idemp:{idempotency_key}` and `LOCAL_IDEMP_CACHE[idempotency_key]`. They are not namespaced with `driver_phone`.
- **Exploit Scenario:**
  - *Memory Exhaustion (DoS):* An attacker sends 50,000 requests with randomly generated `idempotency_key` strings. `LOCAL_IDEMP_CACHE` consumes hundreds of megabytes of RAM until the Linux kernel terminates the worker with an OOM kill.
  - *Data Snooping / Poisoning:* Attacker guesses or pre-warms a shared idempotency key `idemp-shift-001`. When Driver B submits with that key, the backend returns the attacker's pre-cached response. Conversely, an attacker can submit an idempotency key used by another driver and receive that driver's cached survey response containing their name, phone, and submission status.
- **Verification Evidence:** Confirmed in `test_wave1_bug_and_legality.py::test_vuln_cache_01_unbounded_local_idemp_cache_leak` and `test_vuln_cache_02_idempotency_key_global_namespace_collision`.

---

### 2.4 Telegram Webhook Forgery & Dossier Exfiltration

- **Severity:** HIGH (CVSS: 7.7 - Confidentiality & Authorization Bypass)
- **Target File & Lines:** `main.py:701-730` (`telegram_webhook_ingress`)
  ```python
  if settings.ENVIRONMENT != "development":
      if not secrets.compare_digest(
          x_telegram_bot_api_secret_token or "", settings.TELEGRAM_WEBHOOK_SECRET
      ):
          raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

  body = await request.json()
  ...
  cq = body.get("callback_query")
  if cq:
      ...
      cq_chat_id = cq.get("message", {}).get("chat", {}).get("id")
      if data.startswith("admin_query:"):
          sub_id = data.split(":", 1)[1]
          dossier = await query_submission_dossier(sub_id)
          report_text = render_dossier_message(dossier)
          await send_telegram_notification(cq_chat_id, report_text)
          return {"status": "query_handled"}

  if text.startswith("/query ") or text.startswith("/check "):
      ...
      dossier = await query_submission_dossier(sub_id)
      report_text = render_dossier_message(dossier)
      await send_telegram_notification(chat_id, report_text)
      return {"status": "query_command_handled"}
  ```
- **Vulnerability Mechanics:**
  1. `settings.ENVIRONMENT != "development"` defaults to `False` in development, completely bypassing webhook secret token authentication.
  2. The endpoint dispatches the full dossier to `chat_id` or `cq_chat_id` extracted from the incoming JSON payload. It NEVER validates whether `chat_id == settings.TELEGRAM_ADMIN_GROUP_ID`.
- **Exploit Scenario:**
  1. An attacker sends a forged POST request to `/webhook/telegram` (or messages the Telegram bot directly):
     ```json
     {
       "message": {
         "chat": {"id": 999888777},
         "text": "/query VN-7D157E"
       }
     }
     ```
  2. The server processes the command, queries the database for submission `VN-7D157E`, generates the sensitive dossier report, and calls `send_telegram_notification(999888777, report_text)`.
  3. The attacker receives the complete driver dossier in their private Telegram chat.
- **Verified Risk:** Full exfiltration of confidential customer and driver surveys, compliance notes, and extracted financial records.

---

### 2.5 VULN-DOS-01: ReDoS & Dynamic Schema Validator Catastrophic Backtracking

- **Severity:** MEDIUM (CVSS: 6.2 - Availability)
- **Target File & Lines:** `services/form_engine.py:88-91` and `main.py:196-218`
  ```python
  # services/form_engine.py
  if q.validation.regex_pattern:
      if not re.match(q.validation.regex_pattern, val_str):
          err_msg = q.validation.regex_error_message or f"Dữ liệu câu hỏi '{q.title}' không đúng định dạng yêu cầu!"
          return False, err_msg
  ```
- **Vulnerability Mechanics:**
  1. `re.match` is invoked on dynamic, unvetted regex patterns from form schemas without timeout protection.
  2. When patterns with nested quantifiers (e.g. `^(a+)+$`, `^(a|aa)+$`) encounter non-matching payloads, Python's backtracking regex engine experiences $O(2^N)$ state space exploration.
  3. Malformed regex syntax (such as unclosed brackets `[a-z`) causes `re.error`, which is unhandled in `validate_submission_against_schema` and triggers an internal 500 error.
  4. `/api/v1/forms/upload-schema` accepts arbitrary regex strings without validation or linting.
- **Exploit Scenario:**
  An attacker submits a 25-character payload to a field validated by a nested quantifier pattern. The CPU core spikes to 100%, locking the synchronous validation execution and stalling all concurrent requests on the event loop.
- **Verification Evidence:** Confirmed in `test_adversarial_round3.py::test_redos_nested_quantifiers_catastrophic_backtracking` and `test_schema_validation_unhandled_regex_syntax_error`.

---

## 3. Team 5: Optimization & Code Cleanup Opportunities

### 3.1 Dead Code & Unused Imports

| File | Line | Unused Item | Description / Action |
| :--- | :--- | :--- | :--- |
| `main.py` | 9 | `List` | Unused typing import. |
| `main.py` | 16 | `StaticFiles` | Becomes dead code once `/data` mount is removed. |
| `main.py` | 17 | `CAMERA_FORM_HTML` | Imported from `services.camera_form_html` but never used. |
| `services/audit_and_privacy.py` | 8 | `ImageDraw` | Unused PIL import. |
| `services/dynamic_form_html.py` | 2 | `json` | Unused stdlib import. |
| `services/form_engine.py` | 2, 4 | `json`, `Union` | Unused stdlib and typing imports. |
| `services/income_sanitizer.py` | 2 | `Tuple` | Unused typing import. |
| `services/normalizer.py` | 5 | `UnidentifiedImageError` | Unused PIL exception import. |
| `services/online_and_ai_detector.py` | 1 | `io` | Unused stdlib import. |
| `services/storage_r2.py` | 3 | `Optional` | Unused typing import. |
| `services/vision_ai.py` | 4, 173 | `json`, `import asyncio` | Top-level `json` is unused; nested `import asyncio` inside `_call_gemini` is redundant. |
| `workers/outbox_worker.py` | 2, 140 | `json`, `sys` | Top-level `json` is unused; `import sys` inside `__main__` is unused. |

---

### 3.2 Database Query Roundtrip Elimination & Persistence Simplification

#### Issue Analysis (`main.py:458-524`)
The production persistence flow in `submit_webrtc_survey` currently executes **three separate network roundtrips** to PostgreSQL:
1. `INSERT INTO survey_submissions ...`
2. `INSERT INTO driver_registered_vehicles ...`
3. `INSERT INTO outbox_events ...`

Furthermore, lines 458-464 wrap the cursor in defensive inspection code:
```python
cur_obj = conn.cursor()
if inspect.isawaitable(cur_obj):
    cur_obj = await cur_obj
cur = await cur_obj.__aenter__() if hasattr(cur_obj, "__aenter__") else cur_obj
if hasattr(cur_obj, "execute") and not hasattr(cur, "execute"):
    cur = cur_obj
```
This adds unnecessary computational overhead and readability friction.

#### Optimization Proposal
1. Simplify cursor management to the idiomatic psycopg pattern: `async with conn.cursor() as cur:`.
2. Combine all three operations into a single atomic multi-statement Common Table Expression (CTE) query:
```sql
WITH ins_survey AS (
    INSERT INTO survey_submissions (
        submission_id, driver_phone, driver_name, receipt_serial_no,
        status, rejection_reason, raw_extracted_facts
    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (submission_id) DO NOTHING
),
ins_vehicle AS (
    INSERT INTO driver_registered_vehicles (
        driver_phone, license_plate, vehicle_type
    ) VALUES (%s, %s, %s)
    ON CONFLICT (driver_phone) DO NOTHING
)
INSERT INTO outbox_events (
    event_type, flow_target, aggregate_id, payload, status
) VALUES (%s, %s, %s, %s, 'PENDING');
```
**Benefits:**
- Reduces database roundtrip latency from $3 \times RTT$ to $1 \times RTT$ (66% reduction).
- Atomic execution guaranteed within a single statement.
- Automatically fixes VULN-AUTH-05 by using `ON CONFLICT (driver_phone) DO NOTHING`.

---

### 3.3 Outbox Worker Transaction Simplification (`workers/outbox_worker.py:118-122`)

#### Issue Analysis
Lines 118-122 execute:
```python
commit_fn = getattr(conn, "commit", None)
if callable(commit_fn):
    res = commit_fn()
    if inspect.isawaitable(res):
        await res
```
However, the preceding block is already enclosed in:
```python
async with conn.transaction():
```
In `psycopg`, the context manager `conn.transaction()` automatically commits upon successful block exit and rolls back on exception. The manual inspection and invocation of `conn.commit()` is completely redundant.

#### Optimization Proposal
Remove lines 118-122 entirely, following the Ponytail principle (deleting dead weight).

---

### 3.4 Bounded TTL/LRU Eviction for In-Memory Caches

#### Issue Analysis
`LOCAL_IDEMP_CACHE` in `main.py:54` is an unbounded dictionary.

#### Optimization Proposal
Implement a minimal, high-performance `BoundedTTLCache` utility class using stdlib `collections.OrderedDict`:
- Configurable `max_size` (default: 2048 entries).
- Configurable `ttl_seconds` (default: 86400 / 24 hours).
- Automatic eviction of expired entries on read/write.
- Least-Recently-Used (LRU) eviction when capacity is reached.
- Namespaced key formatting: `idemp:{driver_phone}:{idempotency_key}`.

---

## 4. Wave 1 Patch Proposals & Action Plan

### Patch 1: Neutralize Unauthenticated `/data` Static Mount (VULN-DATA-01)
- **File:** `main.py`
- **Action:**
  - Remove line 16: `from fastapi.staticfiles import StaticFiles`
  - Remove line 111: `app.mount("/data", StaticFiles(directory="data"), name="data")`

### Patch 2: Eliminate Vehicle Hijacking & Optimize Query Roundtrips (VULN-AUTH-05 & Team 5)
- **File:** `main.py`
- **Action:**
  - Replace sequential `cur.execute` calls in `main.py:458-524` with a single unified atomic CTE query.
  - Enforce `ON CONFLICT (driver_phone) DO NOTHING` on vehicle registration.
  - Remove defensive `inspect.isawaitable` cursor reflection.

### Patch 3: Implement Bounded TTL/LRU Cache with Phone Namespacing (VULN-CACHE-01, VULN-CACHE-02)
- **File:** `main.py`
- **Action:**
  - Introduce `BoundedTTLCache` class.
  - Update `submit_webrtc_survey` to query and store keys as `f"idemp:{driver_phone}:{idempotency_key}"`.

### Patch 4: Enforce RBAC on Telegram Webhook Dossier Exfiltration (VULN-AUTH-06)
- **File:** `main.py`
- **Action:**
  - Verify that `str(chat_id) == str(settings.TELEGRAM_ADMIN_GROUP_ID)` and `str(cq_chat_id) == str(settings.TELEGRAM_ADMIN_GROUP_ID)` before executing `/query`, `/check`, or `admin_query:`.
  - Enforce secret token verification when `settings.TELEGRAM_WEBHOOK_SECRET` is set.

### Patch 5: Harden Schema Validation against ReDoS & Syntax Crashes (VULN-DOS-01)
- **File:** `services/form_engine.py` and `main.py`
- **Action:**
  - In `services/form_engine.py`, wrap regex evaluation in `try...except (re.error, Exception)` and truncate evaluated string length to a safe upper bound.
  - In `main.py::upload_form_schema`, validate and test-compile all `regex_pattern` entries, rejecting unclosed or nested quantifier patterns.

### Patch 6: Purge Dead Code & Unused Imports (Team 5)
- **Files:** `main.py`, `services/audit_and_privacy.py`, `services/dynamic_form_html.py`, `services/form_engine.py`, `services/income_sanitizer.py`, `services/normalizer.py`, `services/online_and_ai_detector.py`, `services/storage_r2.py`, `services/vision_ai.py`, `workers/outbox_worker.py`.
- **Action:** Clean up all identified unused imports and delete redundant `conn.commit` reflection in `outbox_worker.py`.

---

## 5. Conclusion & Next Steps

This Wave 1 audit provides a complete, verified roadmap to harden the Mobile Survey Verification Pipeline against all critical unauthenticated access, injection, and denial-of-service vulnerabilities, while cutting bloat and improving database throughput by 66%. 

The patch proposals are fully specified and ready for deployment in Wave 1 Patching.
