# Survey Pipeline Module Implementation Plans

**Status:** corrected implementation contract; no runtime fix, migration, test result or deployment is claimed by this documentation change
**Baseline reviewed:** `master` at `8e83727`; the original eight-finding review used its parent `494c7e9`
**Primary executor:** DeepSeek 4.1 Flash 1017; record actual provider/model/harness configuration with each wave, rather than treating a display name as capability evidence
**Scope:** harden the existing modular monolith in place, preserving required/existing use cases and repairing confirmed persistence, privacy, delivery, moderation and release-path gaps
**Planning stance:** migration, not greenfield redesign. Implement one independently executable wave at a time. This plan authorizes neither autonomous production deployment nor unresolved product-policy decisions. Its authority, approval and activation gates take precedence over historical documentation.

**Local implementation update (2026-11-21):** Runtime/H1/Wave 3 and the remaining 4A/4B/5/6/7 source boundaries have local implementation. Migration `003_outbox_operations.sql` adds actual worker build/poll evidence to the queue protocol 002; guarded release `--schema 002` requires all three ledger versions. This plan's future-tense baseline is retained as the migration contract, not a claim that the code is absent or activated. [README](../README.md#fresh-pre-push-acceptance-2026-11-21) records fresh full-suite acceptance (**896 passed, 1 optional Python-browser skip**); [DEPLOY_TOOLING.md](DEPLOY_TOOLING.md) records the actual script interface and external gates. No staging/production cutover or observation-window removal has occurred.

## 1. Migration-first framing

The executor must treat the current repository and its deployed data as the starting system. Do not replace the survey route, Telegram flows, admin UI, form registry, or integrations merely because a cleaner design is possible. Upgrade ownership incrementally.

### 1.1 Existing use-case inventory

The inventory distinguishes shipped behavior from broken required behavior. Preserve supported contracts; do not preserve a disclosure or describe an acknowledgement-only callback as a completed business operation.

| Use case | Current entry point and behavior | Migration obligation | Target owner after cutover |
|---|---|---|---|
| Driver/CTV opens a form | `GET /forms/{form_id}`, `/camera-form`; `routers/survey.py` renders dynamic HTML from `FORM_REGISTRY` | Preserve URLs, referral/CTV selection, conditional questions, consent, GPS UX, camera slots, and form JSON | HTTP adapter -> form registry/rendering services |
| Driver submits a live survey | `POST /api/v1/survey/submit-webrtc`; multipart legacy and indexed photo names are accepted | Preserve all documented fields, test mode isolation, approval/rejection/manual-review statuses, response JSON, and Vietnamese reasons | HTTP adapter -> submission use case |
| Driver receives a verdict or requests help | Two distinct submission notifications: admin review card and configured question/CTV verdict; Telegram support and active appeal locks | Preserve both recipient classes, phone linking, callback identifiers and relay isolation; delivery ownership and receipts are per recipient/transition | notification/outbox handlers + existing Telegram runner |
| Driver resubmits or appeals | New form attempt; `POST /api/v1/survey/appeal`, `/api/v1/survey/appeal/cancel`; Telegram appeal/repair/cancel flows | Distinguish uncertain retry from deliberate resubmission; preserve terminal/open-state rules and section 1.7 quotas | submission/appeal owners backed by PostgreSQL; test adapter explicitly separate |
| Admin reviews and moderates | Approve/reject update status and notify; vehicle-change and warning callbacks currently only acknowledge | Preserve callback strings and confirmation UI; implement actual binding/history and truthful warning outcomes; reconcile actions skipped under an earlier verdict | moderation transaction + durable downstream/notification actions |
| Admin inspects submissions | `/admin/submissions`, `/admin/data`, `/api/v1/submissions`, authenticated photo endpoints | Preserve filters, summaries, form metadata, all photo readers and dev-only inspection; use Wave 4's private media access matrix | admin HTTP adapters -> repositories/read models |
| Admin retries downstream work | `GET /api/v1/submissions/{submission_id}/actions`, `POST /api/v1/submissions/{submission_id}/retry-actions`; currently schedules `dispatch_action_checklist` | Keep paths, auth and response semantics; requeue durably without a competing provider owner | action/outbox repository and worker |
| Admin manages forms | `/admin/forms`, create/edit/upload-schema, form registry refresh | Preserve schema editor behavior, file persistence, conditional logic, image-object/check-engine contracts, and live refresh semantics | form-management adapter + schema sync |
| Admin manages media and guide images | `/admin/media`, `/api/v1/media*`, static `/media` | Keep guide/form/general media separate from submission media; preserve path safety and admin auth | media management service; submission media has a separate durable manifest |
| Admin manages CTVs and audit data | `/admin/ctvs`, `/api/v1/audit/logs`, audit UI | Preserve directory updates, quotas, audit visibility, and authorization | repositories/read models + moderation services |
| Telegram webhooks and bot operations | `/webhook/telegram`, `/webhook/telegram/admin`, `/webhook/telegram/driver`; polling runners | Preserve secret validation, driver/admin role separation, relay isolation, and idempotent update handling | webhook adapters -> existing bot runners; durable side effects where applicable |
| Lark ingress | `POST /webhook/lark-survey`; signature/challenge/replay checks and synchronous verdict path | Preserve handshake, signature validation, nonce behavior, image checks, verdict response, and migrate R2/background work safely | Lark adapter -> submission/evaluation use case -> outbox |
| Customer delivery | `services/lark_base_sync.py`, `services/google_drive_delivery.py`, manual `/api/v1/admin/sync-drive` | Preserve approved-only policy, sanitized fields, image layout, manual sync capability, and idempotent reruns | real outbox handlers plus an explicit admin requeue/sync command |
| Development/test workflows | `is_test_mode`, `data/dev_submissions.json`, `/api/v1/dev/submissions` | Keep isolated test storage and admin-only inspection; never allow it to become an implicit production fallback | explicit test adapter only |
| Runtime/operator health | `/health`, deployment smoke scripts, app and worker containers | Preserve liveness checks, add readiness without changing liveness semantics, and make worker health visible | runtime dependency/readiness module |
| Driver inspects history | Public form modal calls `GET /api/v1/surveys/history` anonymously; tests disagree on 200 versus 401 | Close disclosure with the H1-approved feature disposition; never inject admin credentials into the form | admin endpoint; verified driver history only under a separately approved identity contract |
| Driver checks verdict or pre-checks a photo | `GET /api/v1/survey/status/{submission_id}`, `POST /api/v1/survey/pre-check` | Preserve PII-free status, polling/reload/backoff, rate limits and pre-check behavior | existing adapters backed by committed state |
| Admin edits the form/image system | Form rename/delete/archive, client-only drafts, image-object/check-engine CRUD, cross-worker refresh | Preserve schema roles, camera grouping, gallery-per-slot settings, custom checks and historical delivery after edits | existing owners; versioned delivery policy snapshots |

### 1.2 Compatibility rules

1. **Add before switching.** Add tables, columns, handlers, adapters, and feature flags before changing the current writer or reader.
2. **Read old and new during transition.** Existing `action_status`, legacy `ALL` outbox events, old media directories, and existing submission rows must remain inspectable while new records use the upgraded representation.
3. **Write one authoritative record.** Dual-write only when the second write is an explicitly temporary compatibility projection. Never let two queues independently execute the same provider action.
4. **Cut over by use case, not by file.** A change is ready only when the driver submission flow, admin viewer, retry endpoint, worker, and operator smoke path agree on the new contract.
5. **Keep identifiers stable.** Existing `submission_id`, form IDs, Telegram callback data, CTV codes, and database primary/foreign-key relationships remain stable. Add mappings only for genuinely new IDs.
6. **Preserve historical data.** Migrations are additive and restartable. Backfills must be resumable, bounded, auditable, and safe to run twice. Never rewrite or delete approved/rejected history as part of a code cutover.
7. **Flag behavior at the owner boundary.** A flag may select legacy versus upgraded persistence/dispatch, but both branches must return the same public response. Do not scatter flags across every route branch.
8. **Rollback means stop new writes, drain safely, and keep reads alive.** Every migration wave below names the old owner, new owner, observation period, and removal condition.

### 1.3 Required migration phases

Use these phases for every module, even when a wave is small:

1. **Inventory:** identify current callers, stored representations, side effects, and operator actions.
2. **Additive schema/code:** add the new representation and adapters without changing the current path.
3. **Backfill or bridge:** copy existing rows/files/events with stable IDs and a progress marker; verify counts and hashes.
4. **Shadow/read comparison:** compare old and new results without changing the user-visible result. Log only redacted differences.
5. **Controlled cutover:** enable the new owner for one use case or environment at a time.
6. **Soak and drain:** observe retries, pending work, admin actions, and historical reads; drain legacy work explicitly.
7. **Removal:** delete the old writer only after no live caller uses it and rollback evidence is preserved.

No plan section may jump directly from current code to removal. If a component has no useful dual-read period, document why and make the change a reversible flag or an additive adapter instead.

### 1.4 Migration ledger for current state

Use this ledger to preserve existing records, clients and workers. Wave 7's release-path SAFETY prerequisites must land or automatic deployment must be gated before any earlier wave can be pushed; its final cleanup still follows correctness work. The activation matrix in Wave 7 defines each valid intermediate state.

| Wave | Existing owner/state | Additive bridge and migration | Cutover gate | Rollback / retained compatibility |
|---|---|---|---|---|
| 0. Baseline | Existing forms, API routes, Telegram/Lark paths, PostgreSQL rows, dev JSON, media directories, and `action_status` values | Record endpoint/form fixtures, representative DB row counts, pending/processing/failed outbox counts by target, old `ALL` count, media-file counts and hashes, worker deployment status, and admin retry/manual sync behavior. Store no production PII in test artifacts. | Staging baseline can replay current supported use cases and identify pre-existing failures before code changes. | No runtime change; retain baseline report for comparison. |
| 1. Runtime safety | `/health` is liveness-only; startup may leave the app running without PostgreSQL; config defaults remain loadable | Add dependency readiness and production validation while keeping `/health` and test mode. Ensure deployment probes can switch from old health semantics only after new readiness is available. | Production startup and submission fail closed, while test-mode and existing health clients retain their contract. | Roll back probe selection, not fail-closed submission or secret rejection. Keep `/health` indefinitely as liveness. |
| 2. History privacy | Public phone lookup/modal; conflicting history tests | Add admin auth and H1-approved UI disposition together; preserve authorized response shape | Anonymous denial before storage/rate-limit effects; real form behavior tested; H1 recorded | Never restore disclosure or give a public client admin credentials; retain PII-free status lookup. |
| 3. Idempotency / writes | Cache-only keys, browser key rotation, independent crash writer, legacy `ALL`/background work | Apply migration 001; persist the browser attempt key; normal/recovery writes share the atomic reservation owner; retain LEGACY work in this intermediate wave | Real PostgreSQL/browser proof of one attempt and uncertain-commit recovery; 001 precedes writers | Retain schema, committed attempts and corrected client/recovery contract. Do not roll back to cache-only identity. |
| 4. Outbox and media | Legacy ALL, JSONB projection, private photos, background producers | 4A installs 002/compatible workers with producers OFF; 4B transfers one producer/cohort, all effects for each new record together. Preserve private media and its adapter. | Activation matrix and both recipient classes, appeal/cancel, moderation and Drive pass; legacy effects reconciled | Pause claims/producers; retain compatible app/worker. Never race background effects with durable-owned records or restart a placeholder worker. |
| 5. Claims/status | `action_status` JSONB and `acquire_action_lease` currently represent status/lease; action endpoints read JSONB | Treat outbox rows as claim authority. Backfill a read projection for legacy statuses, keyed by submission/action; never infer a provider succeeded only from a legacy `RUNNING` state. Preserve old JSONB for admin reads during transition. | Action endpoint returns equivalent or more precise statuses; concurrent claim test has one winner; retry endpoint queues work durably. | Restore legacy status display only, not legacy production claim writes. Preserve original JSONB and new outbox rows for reconciliation. |
| 6. Use-case extraction | Survey HTTP and Lark webhook paths have distinct ingress/orchestration; admin moderation, manual Drive sync, and appeal flows call existing functions directly | Add adapters around existing functions first. Migrate one caller at a time: WebRTC submission, Lark ingress if it has a persistence contract, admin retry, moderation side effects, then manual sync. Keep function signatures/response mapping at old call sites until parity tests pass. | For each caller, compare old/new verdict/status and side-effect policy in staging; no duplicate notifications, exports, billing, or moderation audit events. | Switch caller back to adapter's legacy implementation behind one owner-level flag; do not run old and new side-effect paths concurrently. |
| 7. Operations and removal | Deployment scripts/Compose may omit worker; historical rows/files and API clients remain in circulation | Add worker supervision and readiness checks. Drain or quarantine old queue rows; record final counts. Remove compatibility writers only after one observation window and caller inventory show zero old writes. | All use-case matrix rows pass; no legacy writes or unclaimed legacy events remain; restore/rollback procedure is rehearsed in staging. | Keep additive schema, historical data, old read projections, and `ALL` compatibility as long as rollback may require them. Destructive cleanup needs separate approval and backup. |

#### Data migration constraints

- No table/column/data deletion, destructive type change or file deletion is part of these waves. Migration 002 may transactionally replace an outbox CHECK constraint only to widen accepted targets, after inspecting its actual definition and validating existing rows. This is not permission for a blanket `DROP` or status rewrite.
- Backfills must be restartable and idempotent. Persist progress by stable primary key or deterministic cursor; report scanned/created/skipped/conflict counts.
- Compare source and target totals, per-status/action counts, and media SHA-256 hashes. Investigate every mismatch before enabling writes to the new representation.
- Do not blindly replay legacy `ALL` events: current worker code can mark placeholder branches processed. First classify each row by age, status, target, submission approval state, `action_status`, ledger row, and existing provider reference. Ambiguous events go to an explicit operator reconciliation report, not automatic replay.
- No backfill may send Telegram/Lark/Drive/Zalo messages or create billing side effects until the corresponding stable provider idempotency/reconciliation method is proven.
- Preserve `data/dev_submissions.json`, `data/submission_photos/`, `data/media/`, and existing PostgreSQL records during compatibility windows. Dev data deletion remains an explicit admin operation, not a migration side effect.

### 1.5 Use-case acceptance gate

Before removing a legacy path, pass section 1.8 and the owning wave's negative tests. Record fixture/test, command, observed result, attempt/submission/action IDs, visible response and durable state. Future test commands become runnable requirements when implemented; their inclusion here is not a claim that the suites exist or pass.

### 1.6 Authority and approval gates

Source establishes observed behavior, not automatic product approval. This plan and the handoff govern this hardening migration. Older architecture, internal-system, operations, testing and roadmap documents supply requirements/history only as dispositioned here; authority labels, performance figures and completion claims do not override this contract.

| Decision | Frozen migration disposition | Approval / evidence required |
|---|---|---|
| H1: driver history and phone delivery | APPROVED: admin-authenticated web phone history; public modal disabled with truthful maintenance text; PII-free submission-ID status unchanged. Verified phone mapping authorizes Telegram delivery/private bot history only, never web access. | Local implementation/checks: [WAVE2_H1_PHONE_BOUNDARY.md](WAVE2_H1_PHONE_BOUNDARY.md). No verified web self-service, public Basic Auth, OTP/SMS or deployment is authorized. H1 is no longer an unresolved policy gate. |
| Quotas | Preserve section 1.7's observed per-channel limits/counters. Do not unify channels or adopt historical 3/8h during hardening. Fix misleading channel-specific message text. | Any numerical, identity-scope or counter-persistence change is a separately approved policy change with tests. |
| Notification ambiguity | Persist per-recipient outcomes. Safe replay is provider-specific; ambiguous Telegram/Lark sends are quarantined for reconciliation/explicit resend, not called exactly-once. | Wave 4 proves post-send/pre-ack handling and visible `DELIVERY_UNCERTAIN`; an explicit resend records duplicate risk. |
| Test-mode authority | Preserve labelled DEV/test operation with separate storage and no production billing/customer export; never missing-DB fallback. | Before Wave 1 production rollout record the server-side form/environment/operator policy permitting test mode. Reject a production request's test/bypass flag if no policy authorizes it; do not relabel it as persisted production success. |
| Required moderation | Vehicle-change/warning acknowledgement stubs are broken required behavior. Repair them in Wave 4 before claiming those use cases delivered. | Committed binding/history or warning audit and truthful delivery state, including missing candidate/unlinked recipient cases. |
| Future roadmap | Zalo, BI, new payroll products, historical performance targets and removed privacy commands are not implicit scope. | Separate scope/acceptance approval; do not revive removed features or enqueue placeholder targets. |

### 1.7 Per-channel quota specification

These are the reviewed implementation's migration baseline, not a claim that counters survive restart or an approval of historical spec values.

| Channel | Limit/window and identity | Counter/active-request semantics | Evidence and acceptance |
|---|---|---|---|
| Web appeal | 20 attempts / 3,600 seconds; supplied phone or `ANONYMOUS` | Redis fixed window from first increment; fallback rolling timestamp list. Terminal/open-state checks precede consumption. | `routers/survey.py:2208-2375`; exact-boundary, expiry and Redis-failure tests in `test_appeal_flow.py`. Preserve the documented fallback difference; web text must say 20/1h, not 5/2h. |
| Telegram appeal | 5 attempts / 7,200 seconds; linked phone else user ID | Per-runner rolling list; reject unknown/approved/manual-review/open appeals before adding an attempt; prune stale locks against stored status. Not Redis-durable. | `services/telegram_polling.py:1684-1774`; `test_driver_bot.py`, `test_appeal_flow.py`; fake-clock tests for attempt 5/6, expiry and active rejection. |
| Telegram support | 5 requests / 7,200 seconds per user ID | Separate rolling list from appeals; pending/active conversation blocks another ticket; blank text starts a prompt, not a submitted ticket. | `services/telegram_polling.py:1818-1858`; `test_driver_bot.py`, `test_two_way_relay.py`, `test_user_facing_behavior.py`; identity isolation, cancel/close and expiry. |

Form/CTV quotas and pre-check/status/history rate limits remain independent. Historical 3/8h text in `services/templates_vn.py` and older specs must not silently replace this table. Moving process-local counters to durable cross-process enforcement requires a separate explicit policy step; moving delivery does not authorize it.

### 1.8 Required/existing-use-case traceability

Extend existing suites with behavioral/browser assertions; source-string checks alone are not runtime proof. A listed test home is not a passing result. Test filenames in this table are under `tests/`.

| Use case | Required fixtures/assertions | Test home and wave |
|---|---|---|
| CTV/referral, GPS, signature/consent | Active and CTV-disabled form; permitted location fallback, signature clear/confirm, accept/dissent, AND/OR branches and cross-worker refresh | `test_ctv_platform.py`, `test_form_engine.py`, `test_user_facing_behavior.py`, `test_form_registry_refresh.py`; 0/6 |
| Multi-question/slot images | Independent camera groups, shuffled income weeks, configured gallery upload, custom objects/engines, per-slot uniqueness and OCR overrides | `test_image_objects.py`, `test_image_check_configuration.py`, `test_image_checking.py`, `test_multi_image_and_order.py`; 0/6 |
| Attempt/retry/interruption | Canonical phone aliases; same-key replay, changed-payload 409, browser timeout/reload, cancellation, commit-ack loss and no-key compatibility | `test_form_engine.py`, `test_wave2_replication_and_novel_probes.py`; planned `test_submission_idempotency.py`, `test_db_savepoint.py`; 3 |
| History | Anonymous denial, operator read, H1-approved modal, no credential leak; reconcile contradictory 200/401 assertions | `test_form_engine.py`, `test_privacy_and_tg_bot.py`; planned `test_history_privacy.py`; 2 |
| Status/pre-check | Existing paths, PII-free result, reload/reopen, polling backoff, limit expiry and image validation | `test_submission_status.py`, `test_status_bar_browser.py`, `test_image_ingress_normalization.py`; 0/3/6 |
| Appeal/cancel/repair | Lowercase IDs, unknown/terminal/open states, channel quotas, cancel/moderation race, replacement version/hash and visible status | `test_appeal_flow.py`, `test_driver_bot.py`, `test_submission_status.py`; 3/4 |
| Moderation/audit | Two-step decisions, once-only required actions after manual approval, actual vehicle/history mutation and warning audit/delivery | `test_user_facing_behavior.py`, `test_telegram_and_features.py`; planned `test_moderation_durability.py`; 4 |
| Telegram linking/relay/notify | Self-contact identity, trusted standalone phone mapping, command/legacy denial, admin card plus verdict, relay isolation; later per-recipient durable outcomes | `test_phone_links.py`, `test_party_notifications.py`, `test_telegram_phone_path.py`, `test_two_way_relay.py`, `test_driver_bot.py`; H1 trust in 2, durability in 4 |
| Admin data/media/actions | Filters/export, all photo readers, anonymous/path-traversal denial, durable retry acknowledgement, legacy/new projection and dev inspection | `test_submissions_viewer.py`, `test_media_manager.py`; planned `test_durable_media.py`, `test_outbox_worker.py`; 4/5 |
| Form/image management | Create/edit/rename/delete/backup, client-only drafts, object/engine CRUD, cross-worker refresh and queued delivery during form edits | `test_form_registry_refresh.py`, `test_schema_sync_responses.py`, `test_image_objects.py`, `test_form_editor_image_objects_ui.py`; 0/4/6 |
| Customer/internal delivery and Lark | Approved-only sanitized customer output versus internal all-status mirror; all slots/replacements; form folders/CSV/Sheet; durable manual `QUEUED`; periodic overlap; challenge/signature/replay without invented submissions | `test_drive_submission_mirror.py`, `test_google_drive_delivery.py`, `test_drive_delivery_and_form_updates.py`, `test_pipeline.py`; 4 |
| Runtime/database/release | Explicit env/secrets, startup versus later DB loss, real PostgreSQL contention/savepoint/restart, schema gate, worker, mutable-volume retention and rollback | Planned `test_runtime_dependencies.py`, `test_db_savepoint.py`, `test_outbox_worker.py`; disposable staging deployment fixture; 0/1/3/4/7 |

### 1.9 Approved H1 / phone-ownership security boundary

Owner decision: web phone history stays admin-only. Its public modal is disabled with Vietnamese/English maintenance text; no phone-query request or operator credential belongs in public JavaScript. Existing submission-ID status/polling stays PII-free and public. Verified web driver history is deferred until a separate identity contract is explicitly approved.

Question-source delivery uses normalized phone -> VERIFIED standalone mapping -> Telegram user ID. It must not require/fall back to authorized_users, ctv_directory, driver records, raw phone digits, numeric chat-ID answers or arbitrary usernames. Missing verification skips the party send with a redacted diagnostic; the separate admin notification remains scheduled.

Only `verified_contact` and `admin_verified` sources are trusted. Self-contact requires positive integer `contact.user_id == from_user.id == user_id` and successful standalone persistence. Historical contact_share, seeded/getChat, command, unknown, NULL and other sources are NOT proof and are not promoted automatically. `/linkphone` is explicitly unverified; its atomic upsert cannot hijack/downgrade a trusted binding. No new table, verification UI, OTP/SMS or backfill is introduced.

Private Telegram history resolves only the sender's trusted mapping; no mapping means no submission-storage read, and group chats receive no private history. This never grants web history/PII access. Contact verification no longer creates driver/CTV/identity-vault records. See [local implementation and evidence](WAVE2_H1_PHONE_BOUNDARY.md); durable side-effect migration remains Wave 4.

Trusted ownership is not transferable by source label. A proved different-account claim leaves the recorded owner unchanged and pauses the number as `ownership_conflict`; unverified claims cannot cause or remove that pause. Same-owner refresh preserves the verification epoch; a new phone intentionally retains earlier same-account bindings until explicit operator revocation. The latest established trusted binding is the bot-history selection. Operators must authorize/audit expected-owner revocation and fresh re-verification; no admin UI is introduced. Bot history and verdict delivery are limited to the current binding period; original stored/server submission time is mandatory, never moderation/client time. Older or undated records remain admin-only. H1 readiness requires `phone_telegram_links`, and its real-PostgreSQL tests are included in the required CI job. Current evidence and limitations are in [WAVE2_H1_PHONE_BOUNDARY.md](WAVE2_H1_PHONE_BOUNDARY.md); no production rollout or immutable per-record identity model is claimed.


## 2. Executor contract

This document is deliberately procedural. The implementation agent must follow the order below and must not infer target behavior from historical architecture documents.

### 2.1 Rules for the implementation agent

1. Re-read the exact target file and nearby tests immediately before editing it. Line numbers in this document are evidence, not patch coordinates.
2. Work one wave at a time. Do not combine runtime safety, history authorization, idempotency, outbox migration, and orchestration extraction in one unreviewable patch.
3. Keep the existing public contracts unless a section below explicitly changes one:
   - multipart field names for `POST /api/v1/survey/submit-webrtc`;
   - the response keys and status values from that route;
   - dynamic form JSON and `data/forms/` behavior;
   - admin and Telegram callback behavior.
4. Do not use Redis, process memory, `BackgroundTasks`, or a local JSON file as the production source of truth for a committed submission, idempotency, or downstream work.
5. A processed action requires a validated provider/database receipt, not logging. Policy skip uses `status='PROCESSED'`, `result.outcome='SKIPPED'` and a visible reason. `SKIPPED` is a UI/projection outcome, not a new outbox status.
6. Reuse integrations, but upgrade helper return contracts and migrate callers where a Boolean/suppressed exception cannot establish a receipt. Do not add a second provider implementation or confuse internal return types with public HTTP/callback contracts.
7. Do not introduce a service split, a message broker, a new ORM, or a new dependency. This repository is a modular monolith plus a separately supervised PostgreSQL worker.
8. Every non-trivial change gets one narrow regression test that exercises the failure scenario, not a source-text assertion. Run the narrow test after the wave, then run the complete suite once after all waves.
9. Production synchronization/deployment is a separate operator-approved action after staging proof, never part of implementation. BOTH `scripts/deploy_prod.sh` and `.github/workflows/pipeline-ci-cd.yml` must satisfy Wave 7; copying files/restarting only the app is not a schema/worker rollout.
10. If a test contradicts the current handoff, inspect the runtime path and update the test to the intended contract only when the old test pins obsolete behavior. Do not weaken a test merely to make the patch pass.

### 2.2 Required response from the executor after each wave

The agent must report:

- changed files and the reason for each;
- schema migration or data backfill performed;
- exact test command and observed result;
- compatibility impact;
- rollback action;
- unresolved risk, if any.

No claim such as "durable", "atomic", "production-ready", or "idempotent" is valid without the corresponding failure-injection test below.

## 3. Current system map and non-authoritative documents

The current request path is concentrated in `routers/survey.py`:

1. parse multipart input and discover the form schema;
2. group uploaded files and infer a phone from answers;
3. normalize and inspect images, run AI checks, and evaluate deterministic rules;
4. write either `data/dev_submissions.json` or PostgreSQL;
5. insert one `outbox_events` row with `flow_target='ALL'`;
6. schedule `services/action_checklist.py` and Telegram work through FastAPI `BackgroundTasks`;
7. cache the response under an idempotency key and return HTTP 200.

The relevant current defects are evidenced in `docs/MODULE_DESIGNER_HANDOFF.md`:

- `main.py` continues with `db_pool=None` after production database startup failure;
- `/api/v1/surveys/history` accepts a caller-supplied phone without ownership proof;
- the outbox worker logs for R2, Drive, Lark, and Zalo instead of executing them;
- the submission route still owns side effects through in-process background tasks;
- the full insert fallback executes in an aborted transaction unless a savepoint is used;
- idempotency lookup runs before inferred-phone resolution and is not a durable atomic claim;
- `config.py` contains known development credentials and secret defaults;
- `services/action_checklist.py` performs a check-then-write lease race;
- DINOv2 embedding is computed synchronously inside the async route.

`docs/PIPELINE_COMPLETE_ARCHITECTURE_MANUAL.md`, `docs/PIPELINE_OPERATIONS_AND_API_REFERENCE.md`, audit reports, and the roadmap contain useful historical intent but are not implementation evidence. In particular, do not assume that an architecture diagram means the worker, schema, or deployment already has that behavior.

## 4. Target invariants

These invariants are the acceptance boundary for all modules:

1. A production request returns a successful persisted verdict only after PostgreSQL commits the submission and its durable work records.
2. A production instance with unavailable required PostgreSQL is either not started or returns non-ready; it never silently switches to `data/dev_submissions.json`.
3. Phone knowledge never authorizes history. Admin auth and the shipped driver's H1-approved disposition are explicit; public clients never acquire admin credentials.
4. Canonical identity/fingerprint precede idempotency. Same scope/key/fingerprint, including interruption recovery, produces one committed submission/response. Changed fingerprint is 409; deliberate new attempts use new keys.
5. After Wave 4, submissions have independent durable work. Wave 3 explicitly retains legacy `ALL`/background ownership as an intermediate state and does not claim durable delivery.
6. After Wave 4, restart loses no committed work intent. Billing/deterministic object writes are replay-safe; remote outcomes follow provider receipt/uncertainty contracts, not a blanket exactly-once promise.
7. No worker marks an event processed when its integration is only logged or simulated.
8. A lease/claim has one database winner. An expired claim can be reclaimed; a completed claim cannot.
9. CPU-heavy embedding work runs through a bounded executor and does not monopolize the FastAPI event loop.
10. DEV/test storage is explicit, separately labelled and subject to recorded server-side authority; never an implicit production fallback or a source of production billing/customer export work.

## 5. Wave 0 - baseline and test harness

### Goal

Create a reproducible safety baseline before behavior changes. This is preparation, not a permission to rewrite tests.

### Files to inspect/change

- `README.md`
- `Makefile`
- `.github/workflows/pipeline-ci-cd.yml`
- `tests/test_architect_fixes.py`
- `tests/test_wave3_exploratory_and_concurrency.py`
- add focused test modules only when the existing suites do not have a suitable location

### Steps

1. Run the existing narrow suites that exercise persistence, form submission, action status, and runtime behavior. Record failures without changing code.
2. Confirm how tests patch `main.db_pool`, `core.state.db_pool`, Redis, and the FastAPI lifespan. Do not create a second fixture convention.
3. Add shared test helpers only if repeated setup is already present in multiple files and the helper reduces rather than expands the fixture surface.
4. Keep mocked unit tests, but REQUIRE disposable real PostgreSQL/pgvector for migrations, aborted transactions, independent-connection uniqueness, claims and restart tests. Replace the misleading SQLite CI URL. Before any code push can auto-deploy, gate unsafe deployment jobs or land Wave 7's mutable-storage protections; a harness commit must not trigger `rsync --delete` over live data.

### Verification

- `make test` establishes the baseline.
- A focused test can construct the app without external credentials.
- No production or network synchronization is run.
- The implementation must add an isolated PostgreSQL integration target; reject production credentials/hosts and require the test database name to end in `_test`. Use fake providers. SQL/test files referenced below are future implementation artifacts, not files created by this documentation change.
- After Wave 4 implements all four suites, the full mandatory integration command is `UPGRADE_TEST_DATABASE_URL=<isolated-postgresql-url> ENVIRONMENT=test .venv/bin/python -m pytest tests/test_db_savepoint.py tests/test_submission_idempotency.py tests/test_outbox_worker.py tests/test_durable_media.py -q`. Wave 0 instead proves the isolated PostgreSQL fixture and existing baseline; each later wave adds/runs its own named suites before proceeding. Apply migrations to empty/legacy fixtures, use independent connections and fail rather than silently skip when the applicable gate lacks PostgreSQL. Never failure-inject production.

### Rollback

Revert only newly added test helpers or CI edits. Do not alter behavior in this wave.

## 6. Wave 1 - production runtime safety and configuration validation

### Goal

Make required dependencies explicit and separate liveness from readiness.

### Affected modules and call sites

- `config.py`: settings validation and safe defaults
- `main.py`: lifespan pool/client initialization and shutdown
- `core/state.py`: dependency accessors and test injection behavior
- `routers/system.py`: liveness and readiness endpoints
- `docker-compose.yml`: only if the readiness probe or worker command needs an explicit service contract
- `tests/test_runtime_dependencies.py` (new focused suite)

### Implementation steps

1. Require an explicit recognized `ENVIRONMENT` (`production`, `development`, `test`); absence/unknown values must not bypass validation via the current development default. Production rejects built-in admin credentials, missing `APP_SECRET` or fewer than 32 characters, and missing/non-PostgreSQL database URLs. Length is only a minimum; operators supply randomly generated secrets. Do not ban valid container hostnames such as `postgres` as workstation fallbacks.
2. Validate provider credentials only for enabled integrations; never log secrets. Tests/development explicitly inject settings rather than relying on an implicit production-looking fallback.
3. Freeze startup policy: production lifespan raises on required PostgreSQL startup/health failure and closes partial resources. Later database loss yields `/ready` 503 and rejects production submissions with 503. `/health` stays liveness-only. Readiness checks schema/capabilities for the ENABLED writer, not future inactive waves.
4. Add `/ready` with 200/503 semantics, then update probes. Test startup failure as lifespan failure, not an HTTP response from a process that never started; test later dependency loss separately.
5. Apply section 1.6's test-authority gate. Permitted test attempts retain labelled DEV responses/storage and isolated providers; a request flag cannot rescue a failed production write. Reject unauthorized test/bypass flags before processing.
6. Preserve HTTP, Redis, PostgreSQL and executor cleanup after partial startup. Required schema readiness is distinct from optional-provider availability.

### Tests and acceptance

Add tests that:

- instantiate production settings with the built-in password/secret and assert validation fails;
- instantiate valid production settings without exposing the values in logs or exception text;
- PostgreSQL startup failure raises from production lifespan and closes partial resources;
- later DB loss makes `/ready` and production submission return 503 without persisted-success acknowledgement;
- permitted DEV mode works separately; unauthorized production test/bypass flags cannot evade the recorded policy;
- `/health` remains liveness-only; `/ready` is 200 only with healthy dependencies and the enabled writer's schema.

### Rollback

If startup validation blocks an existing deployment, set explicit environment values and deploy the code rollback together. Never restore built-in production credentials. If readiness path integration is wrong, revert only the probe wiring while retaining the no-success-without-commit guard.

## 7. Wave 2 - history privacy boundary

### Goal

Close phone-number enumeration without inventing an unimplemented OTP system.

### Contract decision

H1 is approved: existing admin auth for web phone history and truthful public maintenance disablement. The pre-H1 modal was a real anonymous caller, not an admin client; its phone/query controls and anonymous fetch are now removed, while PII-free submission-status polling is retained. Verified Telegram delivery/private bot history is a separate trust boundary, not web authentication. See sections 1.6/1.9 and the local evidence record; no OTP or public Basic Auth is authorized.

### Affected modules and call sites

- `routers/survey.py`: `get_survey_history`
- `core/state.py`: reuse `verify_admin_auth`; do not duplicate credential comparison
- `static/ui/dynamic_form.html`: actual driver modal, 401/disabled messaging and H1-selected behavior
- `tests/test_form_engine.py`, `tests/test_privacy_and_tg_bot.py`: reconcile public-200/private-401 expectations without losing enumeration protections
- `tests/test_history_privacy.py` (new or the narrowest existing privacy suite)
- `docs/PIPELINE_OPERATIONS_AND_API_REFERENCE.md`: update examples to use credentials without retaining default secrets
- `README.md`: state that history is admin-authenticated
- `core/phone_links.py`, `services/telegram_polling.py`, `tests/test_phone_links.py`, `tests/test_driver_bot.py`, `tests/test_party_notifications.py`: approved standalone verification, source trust, self-contact, bot history and phone delivery; no new driver/CTV dependency

### Implementation steps

1. Add `credentials: HTTPBasicCredentials = Depends(admin_security)` or reuse the exact `verify_admin_auth` dependency already imported by admin routes. The preferred implementation is `Depends(verify_admin_auth)` so there is one policy.
2. Apply authentication before reading the supplied phone, touching development storage, querying PostgreSQL, or incrementing the rate limiter. This avoids turning 401 responses into a phone oracle.
3. Rate-limit the authenticated principal and source where available, not only the requested phone. Retain a bounded secondary phone counter only as abuse telemetry; it is not an ownership check.
4. Keep response shape for authorized operators unless a privacy review explicitly changes it. Do not return history for unauthenticated requests, even when Redis is unavailable.
5. Do not expose raw phone numbers in new logs or errors. Keep existing data lookup behavior behind the authorization boundary.

### Tests and acceptance

- no credentials -> 401 with `WWW-Authenticate`;
- wrong credentials -> 401;
- valid admin credentials -> existing history shape and production/dev aggregation remain available;
- querying another phone with valid admin credentials is allowed only because the endpoint is explicitly operator-scoped, and the docs say so;
- Redis unavailable does not bypass authentication;
- rate limiting is not keyed solely by the requested phone.
- a browser exercises the H1-approved form behavior; 401 is not reported as empty history and no operator secret appears in public assets.

### Rollback

Never restore public phone-history access. Compatible rollback retains auth and the H1-approved UI disposition. Operator clients may use Basic Auth; a public driver client never receives those credentials. Verified self-service requires the separately approved identity contract.

## 8. Wave 3 - durable idempotency and correct submission persistence

### Goal

Resolve canonical identity before lookup, prevent concurrent duplicate submissions, and make the database transaction the only production success boundary.

### Database migration

Implement `scripts/migrations/001_submission_attempts.sql` as an explicit transactional migration; keep `schema.sql` as its fresh-install mirror, not an automatic migrator. `core/schema_sync.py` handles form JSON, not PostgreSQL DDL. Migration 001 creates the idempotency table below, `survey_submissions.media_manifest JSONB NOT NULL DEFAULT '[]'::jsonb`, `media_version BIGINT NOT NULL DEFAULT 1`, and `schema_migrations(version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`. Existing empty manifests use the trusted legacy photo fallback. Apply 001 before enabling its writer.

```sql
CREATE TABLE IF NOT EXISTS submission_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    submission_id VARCHAR(64),
    response_json JSONB,
    state VARCHAR(16) NOT NULL CHECK (state IN ('RESERVED', 'COMMITTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (scope, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_submission_idempotency_expiry
ON submission_idempotency (expires_at)
WHERE expires_at IS NOT NULL;
```

`RESERVED` is transaction-local only: no successful path may commit it. Unique-insert conflicts wait for the transaction, not a separately committed reservation lease. An externally visible RESERVED row is an invariant violation to quarantine, not a lease to steal. Retain committed keys for at least 24 hours; cleanup is disabled by default, is unnecessary for correctness and may later delete only expired COMMITTED rows under lock after an approved retention contract. Never use expiry to steal active work.

### Affected modules and call sites

- `schema.sql`
- `routers/survey.py`: `submit_webrtc_survey`, especially current lookup at approximately 1191-1206 and response cache at approximately 2189-2198
- `core/repositories.py`: add small explicit persistence helpers rather than embedding more SQL in the router
- `core/submission_ids.py` and existing phone hashing helpers, if needed
- `tests/test_submission_idempotency.py`
- `tests/test_db_savepoint.py` or the narrowest existing persistence suite
- `static/ui/dynamic_form.html`: persist the client attempt across uncertain retry/reload; rotate only for a defined new attempt
- `routers/survey.py`: migrate `_submission_crash_guard`, `_persist_interrupted_submission` and media cleanup, not only the normal insert
- `routers/admin_data.py`, `services/admin_moderator.py`, `services/telegram_polling.py`, `services/drive_submission_mirror.py`, `services/google_drive_delivery.py`: ship the private manifest read/replacement adapter BEFORE enabling Wave 3's manifest writer. Legacy work ownership remains unchanged; manifest hashes must not become stale through the old replacement path.
- `tests/test_wave2_replication_and_novel_probes.py`: retain cross-driver isolation while changing obsolete same-key/different-payload 200 assertions to 409
- planned SQL artifacts `scripts/migrations/001_submission_attempts.sql` and Wave 4's `002_outbox_delivery.sql`; this documentation edit creates/applies neither

### Executable schema application contract

Every migration takes the same transaction-scoped PostgreSQL advisory migration lock, verifies its recorded version/checksum, applies DDL and ledger insertion atomically, no-ops for the same completed version and aborts on checksum/schema mismatch. Use `psql -X -v ON_ERROR_STOP=1`; copying SQL is not application. Test each migration twice and inject failure before ledger commit.

Disposable integration/staging command template, usable only after implementation creates the named file:

```bash
: "${UPGRADE_TEST_DATABASE_URL:?use an isolated database ending in _test}"
PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
  psql "$UPGRADE_TEST_DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -v migration_sha256="$(sha256sum scripts/migrations/001_submission_attempts.sql | cut -d ' ' -f 1)" \
  -f scripts/migrations/001_submission_attempts.sql
psql "$UPGRADE_TEST_DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -c 'SELECT version, checksum FROM schema_migrations ORDER BY version;'
```

Use the same command for 002 after 001. The future integration runner validates target isolation before invoking this template. Production uses an operator-approved deployment job with independently verified target/backup, not the test variable or a guessed container name. Both deployment paths apply the required version before enabling dependent writers; readiness reports mismatches. Historical operations examples do not authorize applying the whole legacy schema blindly.

### Canonical request sequence

1. Parse multipart once, group uploads and resolve form/mode under server-side authority.
2. Resolve/normalize explicit or inferred phone once. Scope includes mode, form ID and an HMAC canonical identity, never raw phone. Use that identity consistently in response and persistence; phone knowledge is not authentication.
3. Read upload bytes once. Fingerprint versioned, sorted UTF-8 JSON of schema-normalized answers with canonical phone/CTV once, form/mode, ordered `(question_id, slot_index, SHA-256(bytes))` and signature content hash. Replace signature/file transport answers with hashes; exclude the key, duplicate aliases, upload placeholder strings, runtime timestamps and model outputs. Keep the GPS/signature/media snapshot stable on uncertain retry. Fixed fixtures make equivalent explicit/inferred phones agree.
4. Browser: use a native-crypto high-entropy key per logical attempt; persist key/fingerprint metadata separately from sensitive media. Reuse on timeout/retry/reload. Never regenerate before each POST. Rotate for a deliberate new submission/confirmed payload edit only after the previous outcome is definite, or an explicit separate-new-attempt decision acknowledges the unresolved prior attempt. If reload loses files, require the original payload or surface unresolved-attempt/support recovery, not silent rotation. Raw-photo persistence needs separate privacy approval.
5. Preserve optional-key API compatibility: a no-key request gets one server-side attempt with NO cross-request dedupe guarantee; the first-party client must supply a key. Anonymous keyed scopes require unguessable keys. Different keys are not merged merely because answers/images match.
6. In ONE transaction insert `(scope, key, fingerprint, state='RESERVED')` with unique-conflict handling and bounded lock wait. After the competing transaction ends, its COMMITTED row with matching fingerprint returns the stored response; different fingerprint returns 409 without the stored private response. Lock timeout returns controlled 503/Retry-After, not a fresh key/row. A visible RESERVED row fails closed for reconciliation; there is no external RESERVED wait/takeover branch.
7. Insert submission/required identity, private manifest, final response and wave-appropriate work, then mark COMMITTED before the one commit. Wave 3 still writes the legacy `ALL` event, not Wave 4 targets. Required new columns are schema-gated, not optional fallback data.
8. Return persisted success only after commit. Caches mirror verified committed responses; they cannot override fingerprint/scope validation. A pre-upgrade response whose fingerprint cannot be established is not authoritative.

### Interruption and uncertain-commit ownership

The shielded interruption helper is currently a second submission writer. Migrate it in Wave 3: normal completion and recoverable interruption use the SAME `(scope, key, fingerprint)` owner. If evaluation fails after media reception but PostgreSQL is healthy, that owner may commit one MANUAL_REVIEW interruption result, response and policy-required legacy work. This explicitly replaces independent crash-row insertion; cancellation itself is not another success acknowledgement.

On cancellation, finish or roll back the active transaction under a bounded shield and retain its attempt/media snapshot. A recovery transaction reacquires the same unique identity: use an already committed response, or commit the single interruption result. Never insert the old submission ID outside reservation ownership. A concurrent retry may win, but both paths resolve to its one committed row; do not change fingerprint to encode the recovery reason.

After a lost commit acknowledgement, query the durable key through a fresh connection before another write. If the database stays unavailable, retain uncertainty and media; do not infer rollback, delete files or allocate a new attempt. Abrupt process death before commit can leave an orphan directory, not a promised committed row. Cleanup needs age plus proof of no committed/active/recovery reference, and remains disabled while outcome is uncertain.

### Savepoint and insert policy

The current full insert catches any exception and immediately runs a fallback insert in the same transaction. Fix this by either:

- using a savepoint around the optional extended insert, rolling back to that savepoint before the minimal insert; or
- removing the broad fallback after the schema is proven current and handling only a narrowly identified compatibility exception.

Choose the smallest safe option. If the fallback remains:

- use `SAVEPOINT survey_extended_insert`;
- on the expected compatibility error, `ROLLBACK TO SAVEPOINT survey_extended_insert`;
- execute the minimal insert;
- release/commit the outer transaction;
- do not hide connection, serialization, or constraint errors as compatibility errors.

The fallback must preserve idempotency and wave-appropriate work. A fake cursor may test control flow, but acceptance needs a REAL PostgreSQL server-side statement error inside a savepoint, then a committed minimal row, COMMITTED reservation and legacy `ALL` event. Connection/constraint failures are not optional-column compatibility errors.

### Tests and acceptance

- repeated request with the same phone inferred from answers returns one `submission_id`;
- repeated request with explicit and inferred forms of the same canonical phone share one record;
- two concurrent requests in separate pool connections produce exactly one submission and one committed idempotency row;
- same key with changed answers or image bytes returns deterministic 409;
- Redis unavailable does not create duplicates;
- a server-side extended insert failure rolls back only to the savepoint and allows the minimal record plus outbox rows to commit;
- database failure before commit returns no successful persisted verdict;
- successful production response is impossible unless the transaction committed.
- browser lost-response retry retains its key and resolves to the first committed ID; deliberate new attempts rotate keys;
- cancellation, evaluation failure and commit-ack loss share reservation ownership and preserve reviewable media, without a second crash row;
- no-key clients retain compatibility without an invented dedupe guarantee; DEV/production/form scopes cannot collide;
- fixed fingerprints cover explicit/inferred aliases and changed answers/signature/GPS/bytes; mismatches never expose another stored response.

### Rollback

Apply/verify 001 first. Wave 3 runs independently with legacy `ALL`/background delivery retained and explicitly NOT certified durable; Wave 4 closes that existing gap. Never activate new targets early. Rollback retains 001, committed keys, private media and corrected recovery ownership. An empty table is allowed; absent/mismatched schema is non-ready. Committed RESERVED is an invariant violation, not normal cleanup work.

## 9. Wave 4 - durable per-action outbox and media references

### Goal

Make PostgreSQL outbox rows the single durable owner of downstream work. Remove the current split between an `ALL` event, in-process `BackgroundTasks`, and placeholder worker branches.

### Design choice

Use `outbox_events` as the queue, with one durable action identity per effect/recipient/transition. 4A installs schema and compatible workers while new producers are OFF; 4B activates them per producer. Only after that producer's 4B cutover must its new submissions stop writing `ALL`. Wave 3/remaining legacy producers keep their explicitly tracked intermediate ownership; never run two owners for one effect.

Implement `scripts/migrations/002_outbox_delivery.sql` after 001. Besides the following columns, add `survey_submissions.work_owner TEXT NOT NULL DEFAULT 'legacy'` and `decision_version BIGINT NOT NULL DEFAULT 0`. Each accepted moderation transition increments decision_version under the submission row lock. Versioned media comes from 001. These are planned additions, not current schema claims.

```sql
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS max_attempts INT NOT NULL DEFAULT 5;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS terminal_reason TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS action_key TEXT;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS aggregate_kind TEXT NOT NULL DEFAULT 'submission';
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS result JSONB NOT NULL DEFAULT '{}'::jsonb;
```

Keep the existing four outbox statuses: PENDING, PROCESSING, PROCESSED, FAILED. Policy skip is PROCESSED plus `result.outcome='SKIPPED'` and a reason; the UI maps that to SKIPPED. Success is PROCESSED plus `result.outcome='SUCCEEDED'` and a validated receipt. Uncertain remote outcome is FAILED with `terminal_reason='DELIVERY_UNCERTAIN'`, NOT a successful/automatically retried send.

Migration 002 widens the actual flow-target CHECK to include `TELEGRAM_NOTIFY`, preserving every legacy target. Inspect constraint definitions, transactionally replace only the narrow CHECK, validate all rows and record the ledger/checksum. This is planned work, not a newly discovered claim that the target already exists. Do not allow new producers until the handler and compatible worker are deployed. Unsupported targets fail visibly, never fall through to PROCESSED.

New `action_key` deterministically includes message/effect class, recipient identity where relevant, decision/media revision and policy version. Add a UNIQUE partial index on `(aggregate_kind, aggregate_id, event_type, flow_target, action_key) WHERE action_key IS NOT NULL`; new producers must supply action_key. Legacy null-key rows stay inspectable until reconciled, not blindly deduplicated. `aggregate_kind` is one of submission, lark_event or drive_sync; bulk jobs must not pretend to be submission foreign keys.

### Durable media contract

Keep the canonical submission store PRIVATE at `data/submission_photos/<submission_id>/`. Never place submission bytes under `data/media`, `/media`, `/static` or any other public mount; `main.py:190-191` currently exposes all `data/media`. This is a proposed-regression prevention gate, not a claim that the unsafe proposed directory was deployed. A known submission ID does not authorize reading photos.

Before commit, normalize and durably write every received slot once. Flush/atomically publish files on the shared durable volume; the manifest records question ID, slot index, flat index, relative path, SHA-256, MIME type and media revision. Reject traversal/symlink escapes after resolving paths against the private root. The database manifest is authoritative for new rows; empty historical manifests resolve only trusted legacy paths.

All consumers use one media adapter. Initial private root files may retain `slot_<flat_index>.webp`; replacements write an immutable revision path, update manifest/media_version transactionally and enqueue only required revision-specific exports. Preserve original evidence; do not overwrite a file referenced by an in-flight action. Old/new adapter compatibility must ship BEFORE the replacement/new manifest writer. A rollback retains that adapter, not a raw-directory reader that silently shows stale revisions.

The app, bot and worker must share the verified durable volume. If they cannot, stop activation until a private object-store reference design with equivalent authorization is approved. A public R2 prefix is not automatically a private submission store. Customer/internal Drive destinations retain their separate approved access policies. Outbox payloads reference immutable manifest revisions, never in-memory byte arguments or client-supplied paths.

Do not unconditionally delete a directory on transaction failure. Follow Wave 3's cancellation/uncertain-commit contract; bounded orphan cleanup is disabled until age and absence of committed/active/recovery references are proven.

### Media authorization and compatibility matrix

| Consumer/entry point | Authority and access | Required negative/parity tests |
|---|---|---|
| Admin list/serve: `routers/admin_data.py` photo endpoints | Existing Basic Auth, safe submission lookup, read current private manifest; historical empty manifest falls back to old private root | Anonymous 401; another known ID does not bypass auth; traversal/symlink denial; every current slot visible after restart/replacement |
| Telegram dossier: `services/admin_moderator.py` | Authorized admin review flow loads the same adapter; do not publish public photo URLs | Unauthorized callback yields no media; dossier matches admin viewer's revision/order |
| Telegram replacement: `services/telegram_polling.py` | Verify bound requester/admin authorization for the specific submission and pending fix; server resolves concrete slot | Wrong user/unknown slot denied; immutable revision/hash, manifest and required work commit together; no false approval on failed write |
| Drive mirror/backfill: `services/drive_submission_mirror.py`, `services/google_drive_delivery.py` | Configured service identity; customer approved-only/sanitized versus internal all-status policy | Rejected/DEV records never enter customer output; internal authorized view remains complete; repaired slot replaces the delivered revision without duplicate rows |
| Outbox worker/R2 | Trusted process with private mount or private-object credentials; references loaded from committed action/manifest | No network/public-file fallback, missing/hash-mismatched media fails visibly, every slot survives app/worker restart |
| Public `/media`, status/pre-check/history and arbitrary URL requests | NO submission-byte read authority, even with a valid submission ID | Known ID plus guessed private/revision URL never returns bytes; public media manager cannot list/delete submission files |

### Affected modules and call sites

- `schema.sql`
- `routers/survey.py`: transaction event creation and all `BackgroundTasks` calls around action checklist and notifications
- `workers/outbox_worker.py`: claim, dispatch, retry, and terminal failure handling
- `services/action_checklist.py`: remove as a production side-effect owner after migration; retain no competing writer
- `services/storage_r2.py`
- `services/lark_base_sync.py`
- `services/google_drive_delivery.py`
- existing Telegram notification functions in the router/service modules
- `services/admin_moderator.py`, `services/telegram_polling.py`, `services/drive_submission_mirror.py`: dossier readers, authorized replacement writer, current revisions and internal mirror
- `static/ui/dynamic_form.html`: attempt identity and H1-approved history behavior remain compatible with the changed owners
- `.github/workflows/pipeline-ci-cd.yml`, `scripts/deploy_prod.sh`, `scripts/smoke_prod.sh`: BOTH deployment paths and worker/schema/readiness gates
- `routers/actions.py`: replace `BackgroundTasks` retry dispatch with durable outbox requeue while retaining endpoint paths and admin auth
- `routers/admin_data.py`: preserve manual Google Drive sync as an operator action; route it through a durable, observable job rather than silently changing its `QUEUED` contract
- `routers/webhooks.py`: migrate Lark submission side effects and any R2/background upload without changing challenge/signature/webhook response behavior
- `routers/admin_data.py`, `routers/admin_media.py`, and `routers/admin_ctvs.py`: retain read/write compatibility for admin UIs while repositories and projections change
- `docker-compose.yml`: add a separately supervised outbox worker using the same application image, network, environment, and media/data volume; do not assume the current Compose file creates one
- `README.md` and `docs/DEPLOY_TOOLING.md`
- `tests/test_outbox_worker.py`
- `tests/test_durable_media.py`

### Submission transaction

For each 4B producer/cohort, commit `work_owner='outbox_v2'`, submission/status transition, immutable media revision and ALL eligible action rows atomically. The scalar owner covers the entire record's required effects: do not split that record between legacy background and durable execution. Canary by producer/environment/new-record cohort, not one effect on the same record. Suppress each matching direct task on committed ownership; action identities distinguish recipients/revisions. Stable targets are:

- `R2_UPLOAD`: enabled, authorized private-media archival using deterministic revision/hash keys; rejected/dissent media must not leak into a public bucket.
- `TELEGRAM_NOTIFY`: separate rows/action_keys for ADMIN_REVIEW, PARTY_VERDICT, APPEAL_OPENED, APPEAL_CANCELLED and configured warning outcomes, each per recipient and transition.
- `LARK_SYNC`: eligible approved records, with stable submission identity and remote-record reconciliation.
- `GDRIVE_EXPORT`: eligible approved customer-mirror action; full manual sync is a distinct `drive.full_sync` event with aggregate_kind=drive_sync.
- `BILLING`: approved collaborator policy, real mapped collaborator foreign key and configured amount; retain ledger uniqueness and never invent a hardcoded payout or treat a missing numeric mapping as paid.
- no Zalo event before a separately scoped real handler/policy.

Optional unconfigured/disabled actions produce explicit policy-skip outcomes, not fake successes. Snapshot effective form notification routing, message class, consent/eligibility and relevant form/media/decision versions when enqueueing. A later form edit must not silently redirect an old verdict to a different recipient. Before export/billing, re-check current moderation eligibility/version so stale queued approval cannot publish or pay after cancellation/rejection. Reconcile rather than overwrite evidence of an already completed external effect.

### Migrate every existing producer, not only WebRTC

These producers all transfer required delivery by the end of Wave 4; Wave 6 only extracts already-correct owners. Do not declare Wave 4 complete while an appeal/cancel or moderation notification remains background-only.

1. **WebRTC:** commit all target rows with the submission. Suppress the old checklist, admin review-card task AND party-verdict task separately for outbox-owned actions. Configured `none` or unlinked recipient is a visible skip with reason, not a completed send; linking/re-routing later creates an explicit new notification action.
2. **Admin retry:** preserve `/actions`, `/retry-actions`, Basic Auth and existing acknowledgement. Requeue eligible FAILED work with an audited operator request, but never retry completed billing or blindly resend DELIVERY_UNCERTAIN. Commit before `retry_dispatched`. Intentional uncertain resend records duplicate risk and preserves previous attempt evidence.
3. **Moderation:** status/decision_version, audit and newly required work share one DB transaction. Re-derive approval actions from CURRENT status/provider/ledger evidence: a historical SKIPPED reason such as 'Rejected submission has no payout' is not terminal after approval. Approval/rejection notifications have distinct transition identities. Preserve two-step confirmation and existing callback strings.
4. **Vehicle/warning repair:** `admin_change_veh` currently only acknowledges. Load the submission's candidate plate/type from authorized stored facts, show it for explicit confirmation and call the real vehicle-change operation; commit binding/history and relevant actions before saying approved. Missing/ambiguous candidate requires input, not fake success. Warning must persist its audit/intended recipient and enqueue configured delivery; an unlinked recipient is reported as not delivered, not 'sent'. Preserve existing callbacks as entry points and add confirmation payloads without trusting a phone embedded in callback data.
5. **Appeal/cancel and Telegram repair:** conditional state transitions plus APPEAL_OPENED/APPEAL_CANCELLED work commit together. Serialize cancel/moderation on the submission row; cancellation cannot silently overwrite a newer terminal decision. Keep section 1.7 quotas and active-session UI semantics. Photo repair uses the media matrix and queues revised approved exports only after authorized media/status commit.
6. **Manual Drive:** one `drive.full_sync` outbox job with aggregate_kind=drive_sync and a real operator job identity, not a fake submission ID or one event per historical row. Preserve existing `QUEUED` response keys only after commit, and expose job ID/outcome through audit/operator inspection. Reuse `sync_all_approved_to_drive`; distinguish local mirror completion from remote customer publication.
7. **Lark ingress:** preserve challenge/signature/nonce/verdict and inventory actual persistence first. Commit required side-effect intents using stable Lark event identity (aggregate_kind=lark_event), without inventing a second survey row. A replay must resolve to the same intent rather than relying solely on process/Redis state.
8. **Periodic Drive:** retain the scheduler and existing database advisory exclusion with manual/full sync. Use the same private media adapter and approved/customer versus internal policy. Versioned upserts and reconciliation must prevent stale per-record work from overwriting a newer bulk result.

Retain support relay session semantics. Any required support-ticket alert included in durable delivery uses the same per-recipient receipt contract; do not claim all transient conversational messages are exactly-once or redesign the chat subsystem incidentally.

### Worker algorithm

Use a short claim/execute/finish loop. Freeze these defaults for initial implementation; changing them requires tests, not an inferred performance target:

1. Claim only new-owner eligible PENDING rows with `available_at <= NOW()` and `attempts < max_attempts`, or safely replayable expired PROCESSING rows, using `FOR UPDATE SKIP LOCKED`. Assign a fresh token, increment attempts ON CLAIM, set locked_at/lease_until and commit before provider I/O. Defaults: 5 claims, 60-second lease, renewal every 15 seconds, one bounded worker process with two execution slots. Do not preclaim more rows than free slots; use target fairness so a bulk Drive job cannot monopolize unrelated notifications/billing.
2. Execute outside the claim transaction. Renew through a separate short token-conditional DB operation. Per-call network timeout is at most 30 seconds; long multi-call sync renews between calls. Lost renewal stops NEW provider calls; an already in-flight call may be uncertain, not magically fenced by a DB token.
3. EVERY success, retry, failure, skip, lease renewal and projection update requires `id`, current claimed_by token and PROCESSING status. A stale worker cannot clear/finish/reset another claim. Keep late provider evidence in audit/reconciliation without overwriting newer state.
4. Validated receipt -> PROCESSED/SUCCEEDED; policy skip -> PROCESSED/SKIPPED with reason. No log-only/Boolean-only or unsupported-target success.
5. Definitive retryable failure increments retry_count and returns PENDING only while attempts < max_attempts. Delay is `min(300, 2 ** (attempts - 1))` seconds; honor provider Retry-After up to 900 seconds. A longer requested wait requires visible operator deferral, never an early retry. Enforce available_at; attempt 5 failure is FAILED, with no sixth claim. Errors are sanitized.
6. Expired replay-safe work can resume only below its attempt limit. A token/row-locked expiry reconciler must finish exhausted rows as FAILED or reconcile an existing receipt, never leave them PROCESSING forever. Expired/ambiguous non-idempotent sends become FAILED/DELIVERY_UNCERTAIN, not automatic resend. A database token does not fence an already in-flight remote call.

### Durable notification and provider reconciliation contract

Current helpers differ: `send_telegram_notification` exposes no outcome and only warns for HTTP 429; `send_telegram_direct_message` returns a Boolean, retries one 429 and logs message_id; its dispatcher checks that Boolean and tries another bot. None exposes a durable final receipt to the worker. Lark returns a Boolean and only logs record_id. Upgrade/reuse these helpers and migrate callers; do not claim they all ignore failure or that awaiting them proves success.

Each handler returns a structured result: SUCCEEDED with provider reference, RETRYABLE with definitive non-acceptance, TERMINAL, POLICY_SKIPPED with reason, or UNCERTAIN. Persist intent and per-claim attempt identity BEFORE calling; persist the validated receipt and local acknowledgement under the token after calling. A sent flag before the call can lose work; one written after cannot rule out duplication after lost acknowledgement.

| Effect / recipient class | Identity and success receipt | Retry / uncertainty / terminal policy |
|---|---|---|
| Telegram ADMIN_REVIEW and PARTY_VERDICT | Separate action per class/recipient/decision revision; snapshot bot role, chat identity and message/markup, NEVER bot token. Require provider acceptance and `(bot identity, chat_id, message_id)`; HTTP 200/True without a valid ID is not a receipt. | Definitive rejection may use the existing alternate-bot policy with each attempt recorded. Timeout/malformed success/missing ID after possible acceptance is UNCERTAIN: no automatic alternate bot or resend. Reconcile or explicit audited resend accepting duplicate risk; operators see per-recipient terminal outcome. |
| Telegram appeal/cancel/warning/support alert | Same receipt contract, keyed by its own accepted state transition/ticket and recipient; cannot collapse into the original verdict's uniqueness key | Partial fan-out acknowledges only recipients with receipts. Failed/uncertain recipients remain independently visible; never replay acknowledged recipients. |
| Lark | Stable submission/event identity; retain provider record_id. On proven existing record, update that record rather than blind create | Preflight lookup alone is not atomic idempotency. Missing/ambiguous/late lookup or post-create timeout quarantines until a single remote reference is established; no blind duplicate create. |
| Drive local mirror, customer publish and internal mirror | Stable form/submission/media revision; receipt explicitly names which stage completed and remote file/Sheet reference where applicable | Local CSV/image write is not remote publication. Reuse advisory exclusion/upsert logic; reconcile partial bulk work and current eligibility. Unknown remote create/update is not successful just because the local helper returned True. |
| R2/private objects | Deterministic submission/revision/content-hash key, expected byte hash and verified object operation reference | Replay only the same immutable bytes/key; no public-prefix fallback for private media. Hash/reference mismatch is terminal or reconciled, not overwrite of a newer revision. |
| Billing | Unique ledger row for submission, real mapped collaborator and amount-policy evidence, committed in DB | `ON CONFLICT (submission_id) DO NOTHING` or equivalent guard; inspect the existing row rather than resetting paid amounts/state. No repeated external payout is introduced by this migration. |

The guarantee is durable work intent plus replay-safe effects WHERE supported, and explicit operator-visible uncertainty elsewhere. Telegram delivery is not promised exactly-once. Reconciliation/manual resend is a real admin action with auth, audit, preserved prior evidence and safe per-recipient identity, not an unimplemented TODO or silent 'completed' status.

### Cutover rules

1. Complete 4A: migration 002, tests and compatible worker code/health exist; new producers remain OFF. Pause old worker claims before the new schema/queue format can be used; never leave a placeholder worker on the same queue.
2. Reconcile legacy ownership before 4B: quiesce/drain the affected legacy background producer, inspect provider/ledger evidence and acquire the submission/ownership row lock. Classify each legacy ALL row. Expand only provably missing eligible actions; quarantine ambiguous already-processed/log-only or RUNNING rows. Existing status-derived SKIPPED is re-evaluated after approval. Record child action IDs and mark the parent reconciled only after durable children or explicit policy reasons exist; never execute the ALL placeholder branch as a success.
3. Canary one producer/use case using committed work_owner/action identities. For its NEW records stop ALL and suppress each matching legacy provider/notification task. Other legacy producers retain explicit old ownership until separately transferred; a deployment flag alone does not establish an effect's history.
4. Verify both admin and party recipients, appeal/cancel/repair, media readers, manual/periodic sync, unknown targets, legacy reconciliation and operator uncertainty actions. Remove raw-byte background arguments only after those gates pass.
5. Make obsolete lease/status writers read-only; JSONB remains a projection, never a second queue. Wave 5 removes compatibility writers after zero old writes are observed.
6. Rollback pauses new claims/producers, preserves queue rows/media and deploys a 001+002-compatible worker/app. Do not restore old placeholder workers or background dispatch for durable-owned rows. Wave 7's matrix defines each retained state.

### Tests and acceptance

- commit a submission, terminate the app before provider calls, start the worker, and observe all required actions finish;
- two workers claiming the same pending row produce one claim winner;
- one failed provider retries without delaying or blocking another target;
- expired replay-safe processing rows are reclaimed; expired/ambiguous non-idempotent sends become visible DELIVERY_UNCERTAIN without a second send;
- retry exhaustion is visible as `FAILED` with a reason;
- R2 reads durable media after app restart and uploads every expected slot;
- repeated billing processing creates one ledger row;
- a placeholder branch cannot be marked processed;
- Telegram notification is not duplicated by both ingress background work and the worker;
- legacy `ALL` rows are handled by an explicit compatibility path and do not create duplicate effects.
- every row/lease/projection transition is token-fenced; stale success AND stale failure cannot overwrite a newer claim;
- attempts are bounded to 5, available_at prevents early retry, heartbeat protects a long job and target fairness allows another effect to progress;
- both admin-card and configured party verdict receive independent receipts; partial failure, alternate bot, accepted-but-no-ID and post-send/pre-ack crash are exercised;
- anonymous known-ID media requests, wrong-user replacement, traversal and public media-manager access cannot disclose/remove submission bytes;
- manual approval re-derives stale SKIPPED policy, produces one ledger row and correct exports; vehicle change actually updates binding/history; warnings never falsely claim delivery;
- apply 002 twice to a legacy fixture; new notification targets work only with the compatible handler; skip remains PROCESSED plus outcome, and legacy rows are preserved;
- form rename/routing edits and replacement revision do not redirect old notifications or publish stale customer data.

### Rollback

Retain schema 001+002 and compatible readers/workers through rollback. Stop claiming/producer activation for a broken target without re-enabling background sends. Preserve per-recipient receipts, uncertain rows, legacy parent/child mappings, media revisions and audit evidence. Destructive schema/data cleanup requires separate approval and backup; no queue format may be exposed to an older placeholder worker.

## 10. Wave 5 - atomic action claims and status projection

### Goal

Close the JSONB lease race and leave one claim mechanism.

### Preferred implementation

The per-action outbox row is the claim record. Do not retain the current `fetch_action_status` -> check -> `jsonb_set` sequence as an active queue. If an existing admin/UI endpoint needs action status, read it from outbox rows or a transactionally updated projection.

### Affected modules

- `services/action_checklist.py`
- `workers/outbox_worker.py`
- `schema.sql`
- any admin route that reads `action_status`
- existing action checklist tests

### Steps

1. Wave 4 already supplies token-conditional outbox claims; Wave 5 removes remaining legacy production `acquire_action_lease` writes only after their caller inventory is empty. It is not permission to defer atomic claims until after new workers are active.
2. JSONB compatibility is a read projection of committed outbox transitions. Fence all updates by action revision/token and never use the projection to decide execution.
3. Preserve UI COMPLETED/SKIPPED/FAILED and retry/uncertain metadata. Map PROCESSED+SUCCEEDED to COMPLETED and PROCESSED+SKIPPED to SKIPPED; never write `outbox_events.status='SKIPPED'`.
4. Remove file-based action lease writes from `data/dev_test_submissions.json` unless a test-only adapter is explicitly needed. Test mode may simulate the state, but it must not be imported by production code.

### Tests and acceptance

- concurrent claim attempts against one event yield one `true` and one `false`;
- a second claim while the lease is live fails;
- expired replay-safe claims can move to a new token; non-idempotent ambiguity follows Wave 4's quarantine/reconciliation contract;
- a processed/completed row cannot be reclaimed;
- status projection cannot overwrite a newer worker state.

### Rollback

Retain read compatibility for the old JSONB shape during one UI migration. Do not restore the check-then-write production path.

## 11. Wave 6 - submission use-case extraction and bounded CPU work

### Goal

Make the HTTP router an adapter and keep expensive image/AI work from blocking the event loop, without changing the form contract.

### Proposed module boundary

Add one application-level module, preferably `services/submission_pipeline.py` or `core/submission_use_case.py`, after the persistence and outbox contracts are stable. Do not create multiple speculative layers.

Define small typed boundaries:

- `SubmissionInput`: normalized form fields, upload descriptors/bytes, mode, request metadata;
- `SubmissionEvaluation`: normalized media, AI facts, deterministic verdict, reason, metrics;
- `SubmissionCommit`: submission ID, persisted status, response JSON, outbox IDs;
- `process_submission(input, dependencies) -> SubmissionCommit`.

The use case must not import FastAPI `Request`, `UploadFile`, `BackgroundTasks`, or HTTP response classes. The router owns multipart parsing, authentication/transport errors, and response mapping. The use case owns validation, media evaluation, persistence, and durable work creation.

### Affected modules and call sites

- `routers/survey.py`: extract orchestration in stages, not a rewrite
- `services/normalizer.py`, `services/vision_ai.py`, `services/evaluator.py`, and existing duplicate/image services
- `core/state.py` and `main.py`: bounded executor lifecycle
- `config.py`: add a bounded embedding worker count only if needed, with a conservative default
- `tests/test_submission_pipeline.py`
- `tests/test_event_loop_progress.py`

### Extraction order

1. Move request-independent normalization of phone, answers, upload descriptors, and form checks into pure helpers with direct tests.
2. Move evaluation orchestration while keeping the existing route's response construction intact.
3. Move the transaction call to the repository/use-case boundary.
4. Move durable media manifest creation and outbox event creation behind the same commit function.
5. Keep required delivery out of route-level BackgroundTasks as already established in Wave 4; extraction must not reintroduce them or defer a missing producer migration.
6. Only then simplify the route and delete duplicate helpers.

### Caller-by-caller migration order

Do not migrate every caller by changing a shared import and assuming parity. Move each caller behind the new owner separately:

1. **WebRTC route:** keep the old `submit_webrtc_survey` adapter and response mapper; invoke the new use case behind one submission-owner switch. Compare status/reason/response in staging, then cut over writes only after Wave 3 idempotency and Wave 4 work records are enabled.
2. **Lark webhook:** retain challenge, signature, nonce, HTTP status, and current verdict response. First extract shared media/evaluation helpers. Only move persistence or outbox work if the current webhook contract actually writes a submission; do not fabricate a `survey_submissions` row from a webhook event without confirming its identity and replay semantics.
3. **Admin retry endpoint:** preserve `GET .../actions` and `POST .../retry-actions` paths, HTTP auth, and response keys. Translate a retry request into an idempotent requeue of eligible failed/pending targets. Return the existing acknowledgement only after the durable requeue commits; rejected/ineligible targets retain their current policy.
4. **Telegram moderation callbacks:** retain callback payload strings and user-visible confirmation. In the same DB transaction as the approved/rejected/vehicle-change status update, create only the downstream actions that changed status requires. Keep audit history and ledger uniqueness. Do not also call the legacy provider dispatch after enqueue.
5. **Manual Drive sync:** retain `/api/v1/admin/sync-drive` and its admin-only behavior. The endpoint may continue to respond `QUEUED`, but only after recording a durable sync request. Keep the existing all-approved sync implementation as the handler; a worker retry must not append duplicate deliverable rows or overwrite newer moderation state.
6. **Appeal/notification paths:** extract the ALREADY durable Wave 4 owner without changing section 1.7 quotas or session behavior. Do not postpone appeal/cancel delivery correctness to this extraction wave.

For each caller, deploy adapter plus old implementation first, run the same fixture through the comparison path without sending external effects twice, then switch that caller. Record a per-caller cutover flag and removal commit. Never share one broad `USE_NEW_PIPELINE` flag across WebRTC, Lark, moderation, and manual admin operations.

### Bounded embedding execution

- Create one lifespan-owned ThreadPoolExecutor with initial max_workers=1 and at most one waiting task; use explicit admission control, not the executor's unbounded internal queue. Shut it down with the app.
- Cover ALL inference paths: direct `compute_dinov2_embedding` at route lines 1660/1911 and synchronous `check_intra_submission_duplicates` at 1639, which calls embedding internally (`services/duplicate_detector.py:155`). Reuse already computed embeddings where valid; moving only the insert-time call is insufficient.
- At saturation return controlled 503/Retry-After without an invented approved/manual-review verdict. Preserve attempt identity and media under the uncertainty contract. A manual-review response is allowed only if genuinely evaluated/committed by the owner.
- Pass immutable normalized image buffers and maintain clear ownership; no mutation of shared request/model state. Do not offload cheap deterministic validation merely for style.

### Tests and acceptance

- a mocked slow embedding allows a lightweight concurrent endpoint to make progress;
- executor concurrency never exceeds the configured bound;
- executor shutdown completes during lifespan shutdown;
- the use case can be tested without FastAPI request objects;
- public multipart field names and response JSON remain unchanged;
- test mode and production mode still use their explicitly separate persistence boundaries.
- slow intra-submission, inter-submission and persistence embedding paths ALL permit concurrent `/health` progress, with one executing/one waiting task and controlled saturation; no hidden default-executor escape.

### Rollback

The extraction can be reverted independently if the adapter contract tests remain green. Keep the bounded executor guard even if deeper extraction is deferred.

## 12. Wave 7 - deployment, observability, and documentation closure

### Required operational changes

- `.github/workflows/pipeline-ci-cd.yml` AND `scripts/deploy_prod.sh`: protect mutable volumes and credentials with an immutable-code allowlist; remove `rsync --delete` over the application/runtime root. Never delete-exclude mutable data. Test retention in a disposable filesystem fixture. The current workflow is a checked-in destructive path, NOT proof that production loss occurred; gate its automatic master/dev deployments BEFORE pushing implementation changes.
- Apply versioned SQL through the verified target connection and check ledger/checksum before dependent writers. Stage and production targets are selected explicitly; do not infer them from a container name or silently apply a production migration to the dev database.
- `docker-compose.yml`/deployment tooling: declare or verify a supervised compatible worker using the actual shared private-media volume and credentials. The checked-in app-dev file is not the production manifest. Pause old queue consumers, deploy the compatible worker, verify its supported targets/schema and fresh successful poll/heartbeat, then activate producers. A running container alone is insufficient.
- `scripts/smoke_prod.sh` and CI: verify app `/health`, `/ready`, worker capability/heartbeat, schema version and non-destructive outbox age/failure/uncertain counts. No live submission, provider send, race test or failure injection belongs in a production smoke check; prove SKIP LOCKED/concurrency in disposable PostgreSQL/staging.
- `README.md`, handoff/archive and operational docs: retain current-versus-target labels and close each finding only after its own observed test/staging evidence. A doc correction or renamed function is not a runtime fix.
- Log commit/idempotency/queue latency, attempts, uncertain/terminal outcomes, worker health and readiness reasons with redaction. Preserve data/forms, form backups, submission photos and revisions, dev files, image-object/check-engine registries, Drive mirrors, model/credential/token files and other runtime state during code synchronization.

### Rollout order

Apply Wave 7's RELEASE-PATH SAFETY prerequisites before any earlier code wave can auto-deploy; Wave 7 final closure/removal still follows correctness work. Each row is an independently verifiable intermediate state, not authorization to deploy automatically.

### Outbox/schema activation and rollback matrix

Schema versions below are planned ledger versions: BASE is the inspected legacy schema; 001 and 002 are the versioned SQL files in Wave 3/4. Required rollout evidence records actual app/worker build IDs, producer state and queue ownership, not merely environment flags.

| Stage | Schema / producer state | Worker, ALL, SKIPPED and notification state | Health/acceptance gate | Retained rollback state |
|---|---|---|---|---|
| 0 / release preflight | BASE; current producers only; unsafe automatic deployment gated | Inventory actual worker; ALL/checklist still legacy; no new target | Baseline fixtures; isolated real PG; mutable-data deployment-retention proof | No runtime/data rewrite; retain baseline/backups; do not ungate unsafe sync |
| 1 | BASE; runtime guards enabled, no queue-format change | No new target; inventory worker is not proof of its correctness | Startup-failure test, `/health`, later-loss `/ready` 503, explicit env/test policy | Keep fail-closed guards; revert only incorrect probe wiring |
| 2 | BASE; history auth and H1-approved UI together | Queue unchanged | Anonymous denial and actual public form/operator tests; H1 recorded | Keep auth and approved UI disposition; never public phone lookup |
| 3 | 001 applied/checksummed; atomic attempt and private-media adapter enabled | Legacy ALL/background ownership retained; TELEGRAM_NOTIFY OFF; no delivery-durability claim | Real PG savepoint/unique/cancel/commit-ack tests; browser retry; reader/replacement parity | Retain 001, attempt records, adapter/media and compatible recovery owner |
| 4A | 001+002 applied; new target producers OFF | Old consumers paused; compatible worker deployed. Legacy ALL is reconciled/quarantined, not placeholder-executed. PROCESSED+SKIPPED understood; Telegram handler capability verified | Target constraint, worker capability/fresh poll, queue/receipt/projection tests; no new side effects during shadow | Keep 001+002; stop claiming; rollback only to compatible worker/app |
| 4B canary | 002; one producer/cohort ON; all effects of each transferred record durably owned | Stop its new ALL/background tasks; Telegram only with deployed handler. Legacy records isolated/drained; recipients independently tracked | `/ready` requires 002 for enabled writer; media/auth, receipt/uncertainty, legacy/restart proofs and worker heartbeat | Pause transferred producers/claims; retain receipts/queue; no legacy dispatch for durable-owned records |
| 4B complete / 5 | 002; all listed required producers transferred; legacy lease writes zero | ALL drained or operator-quarantined with mappings; JSONB projection read-only; no placeholder handlers | Appeal/cancel/moderation/manual+periodic sync matrix, claim fencing and audited reconciliation pass | Retain compatibility reads/schema/media/evidence; no old worker binary |
| 6 / 7 closure | 002; same ownership, extraction only, then bounded removal | No provider-owner change; compatible worker remains supervised | All section 1.8 cases plus bounded inference and safe deployment/rollback rehearsal; full pytest once | Retain private media/history/schema and supported worker; destructive cleanup separately approved |

Deployment sequence within an approved row: gate writes/claims where needed -> verify target/backup -> apply/check migration -> deploy compatible worker/readers -> verify capability/heartbeat -> deploy app -> verify readiness -> enable the allowed producer -> inspect durable outcomes. Production inspection is non-destructive; do not reproduce failure injection there.

### Rollback triggers

Stop rollout and preserve evidence if any of these occurs:

- a production persisted-success response has no committed submission/attempt row;
- one scope/key/fingerprint creates multiple committed submissions, or billing uniqueness is violated;
- PROCESSED lacks either a validated receipt or explicit policy-skip outcome;
- readiness is 200 without required PostgreSQL/schema, or an incompatible/stale worker can claim new targets;
- private media is readable anonymously, disappears during deploy, or its manifest/hash is unavailable after restart;
- history bypasses its auth/H1 contract, or a public asset contains an operator secret;
- an unsafe replay duplicates an effect, an ambiguous send is silently acknowledged, or a stale claimant overwrites current state;
- deployment touches mutable data or enables a producer before the activation matrix allows it.

## 13. Final definition of done

The implementation is complete only when all of the following are observed in tests or controlled staging evidence:

- production fail-closed persistence;
- authenticated history and the H1-approved driver-feature disposition;
- canonical, durable, atomic idempotency;
- savepoint-safe persistence fallback or a justified removal of the fallback;
- real per-action handlers, explicit policy-skip/terminal/uncertain outcomes, per-recipient receipts and audited reconciliation;
- private authorized media available to every reader/replacement/worker after restart and deployment, with revision/hash parity;
- one atomic claim owner with no check-then-write race;
- production secret validation;
- bounded embedding execution;
- section 1.8 traceability and per-channel quota boundaries; actual vehicle-history/warning repair and correct manual-approval actions;
- applied/restartable migration versions, compatible worker supervision, both protected deployment paths and exercised rollback;
- real PostgreSQL concurrency/savepoint/restart gates and browser attempt-retry proof, not mock/source assertions alone.

Documentation correction alone does not close runtime findings or approve production rollout. H1 owner approval and scoped local verification are recorded in WAVE2_H1_PHONE_BOUNDARY.md; separate test-authority/rollout gates remain. Other implementation is complete only with its observed evidence above. Anything unsupported by its own behavioral/failure test remains open in the handoff.
