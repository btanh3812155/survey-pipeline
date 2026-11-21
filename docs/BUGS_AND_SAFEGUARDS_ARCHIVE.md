# Comprehensive Bug Archive & Architectural Safeguards

**Historical Safeguards and Original Eight-Finding Static Review Register**
*Compiled from Git History, OMP Session Archives, Red-Team Adversarial Findings, and Production Incident Logs.*

> **Current upgrade authority:** The summary below preserves its original baseline; it is not a readiness certificate. The source has since gained local runtime/H1, attempt/private-media, durable producer/operator, bounded use-case and guarded-release implementations. Use [README](../README.md) for observed acceptance results, [MODULE_DESIGNER_HANDOFF.md](MODULE_DESIGNER_HANDOFF.md) for baseline versus local status, and [MODULE_IMPLEMENTATION_PLANS.md](MODULE_IMPLEMENTATION_PLANS.md) for required contracts. The full local suite is not green; no deployed-state closure is claimed.

---

## 1. Executive Summary & Safeguards Matrix

| ID | Failure Mode / Bug Category | Historical Root Cause | Impact | Implemented Safeguard & Mechanism | Verification Test |
|---|---|---|---|---|---|
| **BUG-01** | **Schema & Format Desynchronization** | Schema JSON on disk drifted from in-memory Pydantic model (`income_survey.json` vs `DEFAULT_TRUST_BRIDGE_SCHEMA`), dropping `ai_detection_prompt`. | Validation failure on valid inputs; downstream parsers break. | `core/schema_sync.py`: Automated bidirectional schema reconciler with downstream parity validation. | `test_schema_bidirectional_sync_and_parity` |
| **BUG-02** | **Arithmetic Check Bypass** | `net_income and platform_discount` check accepted negative costs (`gross - (-cost) = profit`), allowing inflated profit. | Financial tampering; negative fees approved. | `services/income_sanitizer.py`: Exhaustive math check (`cost_val = abs(c)`, `net <= gross * 1.15`). | `test_exhaustive_arithmetic_check_income_minus_costs_equals_profit` |
| **BUG-03** | **Unwanted Camera Activation on File Upload** | `handleSlotFileUpload()` triggered `selectCameraSlot()` with implicit `activateCamera()`. | Mobile camera hardware turned on unexpectedly when user uploaded file. | Explicit `shouldActivateCamera=false` default parameter; standby state preserved. | `test_multi_image_upload_and_slot_filling` |
| **BUG-04** | **License Plate & PII Exposure in Admin Group** | Admin verdict message broadcast driver license plate and phone number directly into group chat. | Privacy violation under Decree 13; unmasked PII. | Masked PII formatting; license plate stripped from notification card. | `test_telegram_admin_notification_no_license_plate_and_has_review_button` |
| **BUG-05** | **Multi-Admin Support Collision & Session Overwrite** | Active support session was keyed by group chat ID, overwriting previous admin bindings when second CTV requested help. | Admin C's messages leaked to CTV B; cross-convo pollution. | `ADMIN_ACTIVE_CONVERSATIONS`: 1-to-1 isolated admin-to-CTV binding; cross-reply guard. | `test_concurrent_multi_admin_support_routing_isolation` |
| **BUG-06** | **Single Admin Hoarding Multiple Support Tickets** | No concurrency limit per admin; single admin could click multiple tickets and broadcast to wrong chats. | Unresponsive support queues; mixed message relays. | 1-Admin-1-Convo Cap: strict check blocking admins from accepting ticket #2 until ticket #1 closes with `/close`. | `test_one_admin_only_holds_one_support_convo_at_a_time` |
| **BUG-07** | **PostgreSQL `bit(64)` dHash Transaction Abortion** | Inserting dHash as bit string without savepoints aborted the entire PostgreSQL transaction on syntax error. | All survey records and audit events dropped. | Transaction savepoints (`SAVEPOINT`) and pre-cast string validation. | `test_db_savepoint_isolation` |
| **BUG-08** | **Mobile Ghost-Click Re-Opening Signature Modal** | Button possessed both `onclick` and `ontouchend` attributes, triggering 300ms synthetic ghost-click. | Signature modal closed and instantly re-opened on touchscreen devices. | Stripped `ontouchend`, added event debounce and `sigModalCooldown`. | `test_signature_confirm_button_event_wiring_and_cooldown` |
| **BUG-09** | **DINOv2 Duplicate False Positives on Cracked Screens** | 0.88 DINOv2 threshold flagged distinct weeks from same physical phone setup as duplicates. | Legitimate drivers rejected for submitting distinct weekly receipts. | Relaxed threshold to 0.985; dHash tightened to `<= 4`; distinct OCR metrics strictly override visual duplicate. | `test_ocr_override_on_visually_similar_same_phone_screenshots` |
| **BUG-10** | **Delayed / Missed Geolocation Prompt** | GPS prompt only showed a static banner without invoking browser permission on first page load. | Drivers submitted forms without GPS coordinates. | `initGpsBannerBehavior()` calls `fetchGpsSilently()` on page load, popping native OS permission immediately on page 1. | `test_location_prompt_triggers_on_first_page_in_html` |
| **BUG-11** | **Redundant "Tài xế: Tài xế" Default Name** | Legacy database column `driver_name` defaulted to "Tài xế" when form had no driver name field. | Notifications printed awkward `Tài xế: Tài xế (0988111222)`. | Conditional rendering: displays `Số điện thoại: <phone>` unless an explicit non-placeholder name is provided. | `test_admin_dossier_suppresses_redundant_driver_name` |
| **BUG-12** | **Dead Close Button on Support Tickets** | Ticket card included an inline button `[🔒 Đóng phiên chat]` that conflicted with chat slash command `/close`. | Button did not close active session properly; confused admins. | Removed inline button completely; session lifecycle governed cleanly via `/close` command. | `test_support_ticket_markup_has_no_dead_close_button` |

---

## 2. In-Depth Failure Mode Analysis & Architectural Safeguards

### Failure Mode 1: Format & Schema Desynchronization Across Components

#### Symptom & Historical Incident
When updating the survey form schema (e.g. adding `ai_detection_prompt` to image slots or creating new question fields), changes were made to `data/forms/income_survey.json` on disk, but `services/form_engine.py` still held the older baseline. In other instances, python code was updated, but the on-disk JSON loaded at startup by `core/state.py` overwrote the in-memory registry with stale definitions. This caused:
- Schema validation rejections on valid multipart payloads.
- AI vision audit omitting slot-specific prompts.
- Downstream Google Drive sync failing to map columns.

#### Architectural Safeguard: `core/schema_sync.py`
Implemented an automated Bidirectional Schema Reconciliation Engine:
1. **Automated Bidirectional Sync**:
   - `reconcile_and_sync_form_schema(base_schema, disk_path)` compares in-code baseline with on-disk JSON definitions.
   - Any newly introduced property in either definition is dynamically merged and propagated.
   - Missing fields are injected with validated defaults via `normalize_slot_definition()`.
   - The canonical reconciled schema is automatically persisted back to disk, eliminating drift.
2. **Downstream Contract Parity Verifier**:
   - `verify_schema_downstream_parity()` checks that every active schema fulfills all contract invariants required by WebRTC ingress, mobile UI, Admin Moderator, and Google Drive delivery.

---

### Failure Mode 2: Arithmetic Reconciliation & Financial Tampering

#### Symptom & Historical Incident
Drivers or automated scripts could submit payloads where:
1. `gross_income = 10,000,000`, `net_income = 15,000,000`, and `platform_discount = -5,000,000`. The calculation `gross - platform_discount` evaluated to `10M - (-5M) = 15M`, approving inflated net profit.
2. `net_income` exceeded `gross_income` without discount.
3. OCR data in individual weekly slots contained mathematical discrepancies, but were passed without inspection.

#### Architectural Safeguard: `services/income_sanitizer.py` & `routers/survey.py`
1. **Absolute Cost Normalization**: `cost_val = abs(platform_discount)`. Platform fees and deductions cannot be negative to artificially inflate profit.
2. **Profit Ceiling Guard**: `net_income` cannot exceed `gross_income` by more than 15% (allowing modest tip/bonus variance).
3. **Reconciliation Variance**: `abs((gross - cost) - net)` must be within `max(gross * 0.08, 60_000đ)`.
4. **Weekly Loop Verification**: `routers/survey.py` validates each weekly OCR report through `analyze_driver_income()`. If any week violates arithmetic consistency, the submission is rejected immediately with `ARITHMETIC_MISMATCH`.

---

### Failure Mode 3: Support Request Collisions & Concurrency Hoarding

#### Symptom & Historical Incident
1. When multiple CTVs (A and B) requested live support simultaneously, the bot mapped active sessions by the admin group chat ID (`cq_chat_id`). When Admin D replied to B, it overwrote the session of Admin C, routing Admin C's messages to B instead of A.
2. An admin could claim multiple support requests simultaneously, leading to cross-convo contamination and starvation of other tickets.

#### Architectural Safeguard: `services/telegram_polling.py`
1. **Isolated 1-to-1 Admin Binding**: `ADMIN_ACTIVE_CONVERSATIONS: Dict[int, int]` binds `admin_user_id -> ctv_user_id`.
   - Admin C's messages route strictly to CTV A.
   - Admin D's messages route strictly to CTV B.
   - Cross-replies are intercepted and blocked.
2. **1-Admin-1-Convo Cap**:
   - When an admin attempts to claim a second ticket (`admin_reply_chat:<user_id>`), the bot detects the existing session in `ADMIN_ACTIVE_CONVERSATIONS` and blocks the action with an alert modal:
     > *"⚠️ Bạn đang trong phiên hỗ trợ với thành viên khác! Mỗi quản trị viên chỉ được giữ 1 phiên hỗ trợ tại một thời điểm."*
   - Group reply attempts to other tickets are similarly blocked.

---

### Failure Mode 4: Mobile Touch Quirks & 300ms Ghost Clicks

#### Symptom & Historical Incident
On mobile touchscreen browsers (iOS Safari, Android Chrome), modal action buttons (such as the signature confirmation button) possessed both `onclick` and `ontouchend` attributes. Mobile browsers fire `touchend`, and then ~300ms later fire a synthetic `click` event at the same screen coordinates. Because the modal closed on `touchend`, the delayed synthetic `click` struck the underlying button or re-triggered the modal opener, creating an infinite ghost-click reopen loop.

#### Architectural Safeguard: `static/ui/dynamic_form.html`
1. Stripped `ontouchend` from form action and modal buttons; standardizing on synthetic-aware `onclick="confirmModalSig(event)"`.
2. Injected a 400ms modal cooldown guard (`sigModalCooldown`) preventing rapid-fire reopen events.

---

### Failure Mode 5: Anti-Fraud DINOv2 False Positives on Authentic Photos

#### Symptom & Historical Incident
Real GSM drivers often photograph their phone resting on the same dashboard or desk setup. The cracked glass pattern, bezel reflection, and background caused Meta DINOv2 visual embeddings to exceed the conservative 0.88 similarity threshold across different weeks of the same driver, generating false positive duplicate rejections.

#### Architectural Safeguard: `services/duplicate_detector.py`
1. **Empirical Threshold Relaxation**: relaxed DINOv2 threshold from 0.88 to 0.985 (reserving image duplicate rejection strictly for near-identical clones).
2. **dHash Distance Tightening**: tightened dHash threshold to `<= 4`.
3. **Deterministic OCR Override**: if OCR extracts distinct weekly metrics (differing gross revenue, net income, trip count, or weekly dates), the system strictly overrides visual embedding matches and approves the distinct photos.

---

### Failure Mode 6: Immediate Geolocation Acquisition on Page 1

#### Symptom & Historical Incident
Drivers frequently filled and submitted the survey without granting GPS permission because the prompt banner was passive and required manual interaction, leading to missing coordinates in PostgreSQL.

#### Architectural Safeguard: `static/ui/dynamic_form.html`
1. `initGpsBannerBehavior()` unconditionally triggers `fetchGpsSilently()` on initial page load.
2. This invokes `navigator.geolocation.getCurrentPosition()`, which immediately launches the browser's native OS location permission prompt on Page 1.

## 3. Findings From Original Static Review (2026-11-19)

These findings came from read-only inspection of `master` at `494c7e9`; tests and production behavior were not run during that review. Historical line references below are not current code locations. Subsequent local implementations are recorded separately in [README](../README.md), including the failing full-suite gate; production disposition remains open. Detailed original scenarios and acceptance criteria remain in [`MODULE_DESIGNER_HANDOFF.md`](MODULE_DESIGNER_HANDOFF.md).

| ID | Severity | Finding | Primary evidence | Status |
|---|---|---|---|---|
| REVIEW-2026-11-19-01 | P0 | Production may return success without persisting when PostgreSQL is unavailable. | Runtime guard/readiness and real-PG route acceptance; [local evidence](../README.md#observed-local-acceptance-2026-11-20) | Locally verified guard; not deployed |
| REVIEW-2026-11-19-02 | P1 | Baseline unauthenticated phone-history disclosure. | Existing admin auth/public disablement; [verification record](WAVE2_H1_PHONE_BOUNDARY.md) | Locally verified H1 fix; not deployed |
| REVIEW-2026-11-19-03 | P1 | Placeholder outbox and background delivery are not restart-durable. | Durable per-action handlers/producers, receipt/uncertainty and PG restart/operator acceptance | Locally verified for durable cohorts; flags OFF, legacy observation/activation open |
| REVIEW-2026-11-19-04 | P1 | Extended-insert fallback runs in an aborted transaction. | Real-PG route savepoint/minimal insert acceptance | Locally verified; not deployed |
| REVIEW-2026-11-19-05 | P1 | Cache-only/mismatched idempotency allows duplicates. | Atomic PG attempt and SIGKILL/replay cases, canonical cache scope, actual Chromium retry | Locally verified; not deployed |
| REVIEW-2026-11-19-06 | P1 | Production accepts default credentials/secrets. | Runtime production settings/authorization tests | Locally verified; secrets provisioning external |
| REVIEW-2026-11-19-07 | P2 | Action lease check/write is not atomic. | Token-fenced PG claims, canonical projection, audited deferred/exhausted retry and uncertainty recovery | Locally verified; legacy compatibility retained until zero-write observation |
| REVIEW-2026-11-19-08 | P2 | DINOv2 inference blocks the async handler. | Direct use-case and actual concurrent HTTP/event-loop bound/saturation/shutdown tests | Locally verified bound; no throughput SLA or live rollout claimed |

Do not mark an item fixed based only on a refactor or a passing test that does not exercise the failure scenario. Update this table and the handoff when status changes.
