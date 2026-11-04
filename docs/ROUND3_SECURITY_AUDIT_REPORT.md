# Round 3 Defensive Security & Architecture Audit Report

**Target System:** Mobile Survey Verification Pipeline (`/home/ebylxn/projects/survey-pipeline`)  
**Assessment Date:** 2026-11-04  
**Audit Scope:** Dynamic Form Engine, Admin Management APIs, WebRTC Ingestion, Dev Test Storage, Authentication & Secret Handling, Fleet Verification, and Privacy Compliance  
**Auditor:** Automated Defensive Security Audit Agent & Independent Red-Team Security Auditor  
**Primary Reference:** `docs/ROUND3_INDEPENDENT_AUDIT.md`  

---

## 1. Executive Summary

Following earlier Red-Team assessments (Round 1 documented in `ADVERSARIAL_TEST_REPORT.md` and Round 2 in `ROUND2_ATTACK_REPORT.md`), Round 3 evaluated the major expansion of the system into a multi-tenant dynamic form builder, WebRTC real-time capture gateway, driver history lookup, and administrative management console.

An independent, completely unbiased Red-Team Security Auditor conducted an adversarial code audit across all endpoints, service layers, and storage mechanics. The assessment identified **19 distinct security vulnerabilities, architectural logic flaws, and access control breakdowns** across Ingress, Form Engine, Auth, and Architecture.

Most critically, the audit identified an architectural disconnection ("Ghost Engine" syndrome): while sophisticated fleet verification, image deduplication, and financial sanitization services were implemented in `services/`, **none of them were wired into the active WebRTC submission endpoint in `main.py`**.

---

## 2. Complete Vulnerability Summary Matrix (19 Findings)

The following matrix documents all 19 identified vulnerabilities, their severity classifications, affected components, exact locations, and remediation statuses:

| Identifier | Title | Severity | Component | Location | Remediation Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **VULN-AUTH-04** | Unauthenticated Dev Submissions Read & Delete | **HIGH** | Ingress / Dev | `main.py:208-225` | **Planned (P0 - Day 1)**: Attach Basic Auth dependency |
| **VULN-AUTH-05** | Unauthenticated Driver Vehicle Profile Overwrite & Hijacking | **HIGH** | Fleet / DB | `main.py:404-415` | **Planned (P0 - Day 1)**: Restrict vehicle upserts to verified sessions |
| **VULN-CONF-01** | Hardcoded Admin Plaintext Password in Source Code | **HIGH** | Auth | `main.py:94-96` | **Planned (P0 - Day 1)**: Migrate credentials to environment variables |
| **VULN-FS-01** | Arbitrary JSON File Write & Path Traversal in Schema Upload | **HIGH** | Form Engine | `main.py:177-180` | **Planned (P0 - Day 1)**: Whitelist slug regex & realpath containment check |
| **VULN-DATA-01** | Unauthenticated Static Exposure of Sensitive `./data` Directory | **HIGH** | Ingress | `main.py:88` | **Planned (P0 - Day 1)**: Remove public static mount of `./data` |
| **VULN-XSS-01** | Stored Cross-Site Scripting (XSS) in Form Engine & Admin UI | **HIGH** | Form Engine | `dynamic_form_html.py:15,636,637,709`<br>`form_manager_html.py:28,30,37,258,427,504` | **Planned (P1 - Week 1)**: Apply `html.escape` and script-safe JSON encoding |
| **VULN-ARCH-01** | Complete Disconnection of Fleet Anti-Fraud and Compliance Engines | **HIGH** | Architecture | `main.py:277-440` vs `services/` | **Planned (P1 - Week 1)**: Wire tracker, stock detector & income sanitizer into WebRTC |
| **VULN-AUTH-06** | Unrestricted Telegram Webhook Dossier Exfiltration | **HIGH** | Telegram Ingress | `main.py:587-605` | **Planned (P1 - Week 1)**: Enforce admin RBAC verification on Telegram chat ID |
| **VULN-AI-03** | Fail-Open Logic Flaw in AI Vision Fallback | **MEDIUM** | Ingestion | `main.py:360-365` | **Planned (P1 - Week 1)**: Set `is_approved=False` on exception, block vehicle upsert |
| **VULN-DOS-01** | Regular Expression Denial of Service (ReDoS) via Dynamic Patterns | **MEDIUM** | Form Engine | `services/form_engine.py:88-90` | **Planned (P1 - Week 1)**: Use linear-time `re2` or regex complexity validation |
| **VULN-API-01** | Missing Server-Side Schema Enforcement in WebRTC Ingestion | **MEDIUM** | Ingestion | `main.py:277-350` | **Planned (P1 - Week 1)**: Enforce `validate_submission_against_schema()` on API |
| **VULN-CACHE-01**| Unbounded In-Memory Idempotency Cache Memory Leak (OOM DoS) | **MEDIUM** | Ingress | `main.py:52, 302-303, 433` | **Planned (P1 - Week 1)**: Replace unbounded dict with capped Redis LRU cache |
| **VULN-CACHE-02**| Global Idempotency Cache Poisoning & Cross-Tenant Snooping | **MEDIUM** | Ingress | `main.py:295-307, 431-438` | **Planned (P1 - Week 1)**: Scope idempotency cache keys to driver phone |
| **VULN-COLL-01** | High-Probability ID Collision and Silent Drop via 24-Bit Entropy | **MEDIUM** | Ingress | `main.py:293, 392` | **Planned (P2 - Week 2)**: Increase submission ID entropy to 64-bit |
| **VULN-PARAM-01**| Client-Controlled Test Mode & Image Check Bypass | **MEDIUM** | Ingestion | `main.py:311-324` | **Planned (P1 - Week 1)**: Remove client bypass flags; bind test mode to server config |
| **VULN-RACE-01** | Unsynchronized File-Based Concurrency Race Condition in Dev Store | **MEDIUM** | Dev Storage | `main.py:196-206, 220-225` | **Planned (P1 - Week 1)**: Use atomic file replacements and file locking |
| **VULN-PRIV-01** | Incomplete Decree 13 Right-to-Erasure (PII Retention in Vault & Vehicles)| **MEDIUM** | Privacy Engine | `services/audit_and_privacy.py:211-242`| **Planned (P2 - Week 2)**: Hash phone in vault and purge registered vehicle record |
| **VULN-OUTBOX-02**| Complete Absence of Transactional Outbox Publishing on WebRTC | **MEDIUM** | Persistence | `main.py:382-419` | **Planned (P2 - Week 2)**: Insert `outbox_events` inside survey SQL transaction |
| **VULN-ENGINE-01**| Unhandled TypeError in Dynamic Schema Conditional Logic | **LOW** | Form Engine | `services/form_engine.py:73-74` | **Planned (P2 - Week 2)**: Add type guards on conditional membership targets |

---

## 3. Categorized Vulnerability Deep-Dive

### Category A: Ingress Gateway & Developer Storage Endpoints

#### 1. VULN-AUTH-04: Unauthenticated Dev Submissions Read & Delete
- **Severity:** HIGH
- **Location:** `main.py:208-225`
- **Mechanics:** `GET /api/v1/dev/submissions` and `DELETE /api/v1/dev/submissions` lack the `verify_admin_auth` dependency.
- **Security Impact:** Unauthenticated public actors can exfiltrate all stored test submissions (including driver phone numbers, names, declared income figures, license plates, and AI extraction results) or issue a `DELETE` request to wipe audit history.
- **Remediation:** Attach `admin_user: str = Depends(verify_admin_auth)` to both endpoints and disable in production.

#### 2. VULN-DATA-01: Unauthenticated Static Exposure of Sensitive `./data` Directory
- **Severity:** HIGH
- **Location:** `main.py:88`
- **Mechanics:** `app.mount("/data", StaticFiles(directory="data"), name="data")` serves the entire `./data` folder to the public internet without authentication.
- **Security Impact:** Any user can request `/data/dev_test_submissions.json` to dump driver survey submissions or `/data/forms/income_survey.json` to extract internal notification chat IDs and configuration details.
- **Remediation:** Remove the static mount of `./data` immediately.

#### 3. VULN-CACHE-01: Unbounded In-Memory Idempotency Cache Memory Leak (OOM DoS)
- **Severity:** MEDIUM
- **Location:** `main.py:52, 302-303, 433`
- **Mechanics:** `LOCAL_IDEMP_CACHE: Dict[str, str] = {}` stores every idempotency key and response indefinitely without size limits, TTL, or eviction policies.
- **Security Impact:** Rapid automated requests with randomized keys cause unbounded memory consumption, triggering Linux OOM termination.
- **Remediation:** Rely strictly on Redis with `ex=86400` TTL or wrap the in-memory fallback in a bounded LRU cache.

#### 4. VULN-CACHE-02: Global Idempotency Cache Poisoning & Cross-Tenant Snooping
- **Severity:** MEDIUM
- **Location:** `main.py:295-307, 431-438`
- **Mechanics:** Idempotency keys are cached globally under `idemp:{idempotency_key}` without scoping to a driver identity.
- **Security Impact:** Adversaries can pre-seed anticipated keys with fraudulent data or probe keys to harvest other drivers' survey outcomes.
- **Remediation:** Scope all keys to authenticated driver identities: `idemp:{driver_phone}:{key}`.

#### 5. VULN-COLL-01: High-Probability ID Collision & Silent Drop via 24-Bit Entropy
- **Severity:** MEDIUM
- **Location:** `main.py:293, 392`
- **Mechanics:** `submission_id = f"VN-{secrets.token_hex(3).upper()}"` provides only 24 bits of entropy ($16^6 \approx 16.7\text{M}$ values). By the Birthday Paradox, a 50% collision probability is reached after ~4,822 submissions. Database query uses `ON CONFLICT (submission_id) DO NOTHING`.
- **Security Impact:** Conflicting submissions are silently dropped while returning `200 OK` to the driver, causing permanent loss of survey records.
- **Remediation:** Increase entropy to at least 64 bits (`secrets.token_hex(8)`).

#### 6. VULN-PARAM-01: Client-Controlled Test Mode & Image Check Bypass
- **Severity:** MEDIUM
- **Location:** `main.py:311-324`
- **Mechanics:** `is_test_mode` and `bypass_img_check` are unauthenticated form parameters from the client.
- **Security Impact:** Attackers can pass `is_test_mode=true&bypass_img_check=true` with a 1x1 dummy image to obtain instant approval and inject synthetic records.
- **Remediation:** Remove `bypass_img_check` from public endpoints and determine test mode from server configuration.

#### 7. VULN-RACE-01: Unsynchronized File-Based Concurrency Race Condition in Dev Store
- **Severity:** MEDIUM
- **Location:** `main.py:196-206, 220-225`
- **Mechanics:** `append_dev_submission()` performs uncoordinated read-modify-write on `dev_test_submissions.json` without file locking.
- **Security Impact:** Concurrent submissions overwrite each other, causing data loss and JSON decoding errors.
- **Remediation:** Implement file locking (`fcntl.flock`) and atomic file replacement (`os.replace`).

---

### Category B: Dynamic Form Engine & Web Management Console

#### 8. VULN-FS-01: Arbitrary JSON File Write & Path Traversal in Schema Upload
- **Severity:** HIGH
- **Location:** `main.py:177-180`
- **Mechanics:** `POST /api/v1/forms/upload-schema` persists schemas using user-controlled `schema.form_id` in `os.path.join(FORMS_STORAGE_DIR, f"{schema.form_id}.json")`.
- **Security Impact:** Traversal strings (e.g. `../../tmp/malicious`) allow writing arbitrary JSON files outside `FORMS_STORAGE_DIR`.
- **Remediation:** Whitelist `form_id` with regex `^[a-zA-Z0-9_-]{1,64}$` and verify `realpath` directory containment.

#### 9. VULN-XSS-01: Stored Cross-Site Scripting (XSS) in Form Engine & Admin UI
- **Severity:** HIGH
- **Location:** `services/dynamic_form_html.py:15,636,637,709` and `services/form_manager_html.py:28,30,37,258,427,504`
- **Mechanics:** Schema titles, descriptions, and IDs are interpolated directly into HTML markup and `<script>` blocks without escaping.
- **Security Impact:** Injected JavaScript executes within the administrator's browser context, leading to session hijacking and credential harvesting.
- **Remediation:** Apply `html.escape` to all dynamic HTML fields and sanitize script-embedded JSON with `\u003c` replacements.

#### 10. VULN-DOS-01: Regular Expression Denial of Service (ReDoS) via Dynamic Patterns
- **Severity:** MEDIUM
- **Location:** `services/form_engine.py:88-90`
- **Mechanics:** Form schemas allow defining custom regex patterns (`q.validation.regex_pattern`) executed via standard Python `re.match`.
- **Security Impact:** Pathological expressions (e.g. `(a+)+$`) cause exponential backtracking, freezing the Python event loop.
- **Remediation:** Compile patterns using Google's linear-time `re2` library or enforce regex timeouts.

#### 11. VULN-API-01: Missing Server-Side Schema Enforcement in WebRTC Ingestion
- **Severity:** MEDIUM
- **Location:** `main.py:277-350`
- **Mechanics:** `FormSchema` constraints are evaluated in client-side JavaScript, but `submit_webrtc_survey()` never invokes `validate_submission_against_schema()`.
- **Security Impact:** Direct API callers bypass required fields and numeric bounds.
- **Remediation:** Execute `validate_submission_against_schema()` in `submit_webrtc_survey()` before processing media.

#### 12. VULN-ENGINE-01: Unhandled TypeError in Dynamic Schema Conditional Logic
- **Severity:** LOW
- **Location:** `services/form_engine.py:73-74`
- **Mechanics:** Conditional rule with `operator: "in"` testing against a non-iterable target raises `TypeError`.
- **Security Impact:** Unhandled 500 error when processing dynamic forms with misconfigured conditional rules.
- **Remediation:** Add type guards `isinstance(expected_value, (list, set, tuple))` before membership tests.

---

### Category C: Authentication, Access Control & Secret Management

#### 13. VULN-CONF-01: Hardcoded Admin Plaintext Password in Source Code
- **Severity:** HIGH
- **Location:** `main.py:94-96`
- **Mechanics:** Admin credentials `admin` / `Legendofzeus2@` are hardcoded in `verify_admin_auth()`.
- **Security Impact:** Full administrative takeover for anyone with read access to the git repository or container image.
- **Remediation:** Move credentials to environment variables (`ADMIN_USERNAME`, `ADMIN_PASSWORD`) via `config.Settings`.

#### 14. VULN-AUTH-06: Unrestricted Telegram Webhook Dossier Exfiltration
- **Severity:** HIGH
- **Location:** `main.py:587-605`
- **Mechanics:** `/webhook/telegram` handles `/query <id>` commands by dispatching full dossiers without verifying that `chat_id` belongs to an admin.
- **Security Impact:** Any Telegram user can query submission IDs to exfiltrate driver PII, GPS coordinates, vehicle details, and signature URLs.
- **Remediation:** Verify that `chat_id` matches `settings.TELEGRAM_ADMIN_GROUP_ID` or belongs to a user with `ADMIN` role in `authorized_users`.

---

### Category D: Fleet Verification, Anti-Fraud & Architectural Integrity

#### 15. VULN-ARCH-01: Complete Disconnection of Fleet Anti-Fraud and Compliance Engines
- **Severity:** HIGH
- **Location:** `main.py:277-440` vs `services/`
- **Mechanics:** Defensive services (`VehiclePersistenceTracker`, `OnlineStockAndAIDetector`, `analyze_driver_income`, `record_hop_audit`) are never imported or invoked in `main.py`.
- **Security Impact:** In production, the system performs zero vehicle mismatch checks, zero stock image detection, zero income bounds checking, and zero audit logging.
- **Remediation:** Wire the complete verification pipeline into `submit_webrtc_survey()`.

#### 16. VULN-AUTH-05: Unauthenticated Driver Vehicle Profile Overwrite & Hijacking
- **Severity:** HIGH
- **Location:** `main.py:404-415`
- **Mechanics:** Production submissions execute an unconditional upsert into `driver_registered_vehicles` without verifying phone ownership.
- **Security Impact:** Attackers can overwrite legitimate drivers' vehicle profiles with conflicting plates, causing future legitimate surveys to fail compliance.
- **Remediation:** Remove public upsert; bind vehicle modifications to authenticated Telegram collaborator sessions or admin approval.

#### 17. VULN-AI-03: Fail-Open Logic Flaw in AI Vision Fallback
- **Severity:** MEDIUM
- **Location:** `main.py:360-365`
- **Mechanics:** AI exceptions trigger fallback setting `is_approved = True`, setting `rejection_reason` to `NULL` and upserting the unverified vehicle into the database.
- **Security Impact:** Adversaries inducing AI timeouts can bypass verification and register arbitrary vehicles.
- **Remediation:** Set `is_approved = False` on exception and do not upsert vehicle records.

#### 18. VULN-PRIV-01: Incomplete Decree 13 Right-to-Erasure (PII Retention in Vault & Vehicles)
- **Severity:** MEDIUM
- **Location:** `services/audit_and_privacy.py:211-242`
- **Mechanics:** `scrub_driver_pii()` redacts driver names but retains `driver_phone` in `driver_identity_vault` and omits `driver_registered_vehicles`.
- **Security Impact:** Non-compliance with Decree 13/2023/ND-CP; personal identifiers remain in active database tables following erasure requests.
- **Remediation:** Hash or delete `driver_phone` in `driver_identity_vault` and delete corresponding rows from `driver_registered_vehicles`.

#### 19. VULN-OUTBOX-02: Complete Absence of Transactional Outbox Publishing on WebRTC
- **Severity:** MEDIUM
- **Location:** `main.py:382-419`
- **Mechanics:** WebRTC survey ingestion inserts records into `survey_submissions` but never inserts events into `outbox_events`.
- **Security Impact:** Decoupled background workers never trigger payouts, R2 archival, or Zalo ZNS notifications for WebRTC submissions.
- **Remediation:** Insert `outbox_events` rows within the survey submission transaction.

---

## 4. Prioritized Remediation Action Plan

```
+---------------------------------------------------------------------------------------------------+
| Remediation Phase Schedule                                                                        |
+-------+-------------------------+-----------------------------------------------------------------+
| Phase | Priority / Target       | Vulnerabilities Addressed                                       |
+-------+-------------------------+-----------------------------------------------------------------+
| P0    | Immediate (Day 1)       | VULN-AUTH-04, VULN-AUTH-05, VULN-CONF-01, VULN-FS-01, VULN-DATA-01|
| P1    | Core Hardening (Week 1) | VULN-ARCH-01, VULN-AUTH-06, VULN-XSS-01, VULN-AI-03, VULN-DOS-01|
|       |                         | VULN-API-01, VULN-CACHE-01, VULN-CACHE-02, VULN-PARAM-01,       |
|       |                         | VULN-RACE-01                                                    |
| P2    | Architecture & Privacy  | VULN-COLL-01, VULN-PRIV-01, VULN-OUTBOX-02, VULN-ENGINE-01      |
|       | (Week 2)                |                                                                 |
+-------+-------------------------+-----------------------------------------------------------------+
```

### 1. Phase P0 (Immediate - Day 1):
- Add Basic Auth to `/api/v1/dev/submissions` (`VULN-AUTH-04`).
- Remove `app.mount("/data")` from `main.py` (`VULN-DATA-01`).
- Move hardcoded `Legendofzeus2@` admin password to `.env` (`VULN-CONF-01`).
- Enforce alphanumeric slug validation on `form_id` (`VULN-FS-01`).
- Stop unauthenticated overwrites of `driver_registered_vehicles` in `submit_webrtc_survey()` (`VULN-AUTH-05`).

### 2. Phase P1 (Core Hardening - Week 1):
- Wire `VehiclePersistenceTracker`, `OnlineStockAndAIDetector`, and `analyze_driver_income` into `submit_webrtc_survey()` (`VULN-ARCH-01`).
- Enforce Telegram `chat_id` admin RBAC verification on `/query` (`VULN-AUTH-06`).
- Escape HTML and sanitize script-embedded JSON in form rendering templates (`VULN-XSS-01`).
- Switch fail-open AI fallback to `is_approved = False` (`VULN-AI-03`).
- Use Google `re2` or regex complexity limits for schema patterns (`VULN-DOS-01`).
- Execute `validate_submission_against_schema()` on incoming WebRTC submissions (`VULN-API-01`).
- Scope idempotency cache keys by phone and replace unbounded dictionary with Redis TTL (`VULN-CACHE-01`, `VULN-CACHE-02`).
- Remove client-controlled bypass flags (`VULN-PARAM-01`).
- Implement file locks on dev storage writes (`VULN-RACE-01`).

### 3. Phase P2 (Architecture & Privacy Resilience - Week 2):
- Transition submission IDs from 24-bit to 64-bit entropy (`VULN-COLL-01`).
- Emit `outbox_events` atomically during survey persistence (`VULN-OUTBOX-02`).
- Update Decree 13 PII erasure routine to purge `driver_registered_vehicles` and hash identity vault phone numbers (`VULN-PRIV-01`).
- Add type guards on conditional rule operators (`VULN-ENGINE-01`).

---
*Report updated and consolidated from the Independent Red-Team Security Assessment.*
