# Module Designer Handoff

## Review baseline

- Repository: `ebylxn/survey-pipeline`
- Branch and revision checked for documentation correction: `master` at `8e83727`; original eight-finding review: its parent `494c7e9`
- Review type: static source/document inspection; no application tests, live provider checks, migrations or production deployment were run
- This handoff separates observed behavior, broken required behavior, proposed-regression prevention and the target contract. Documentation correction does not close a runtime finding or prove deployed behavior.

Use this baseline with [the corrected implementation plan](MODULE_IMPLEMENTATION_PLANS.md). That plan's authority/approval gates, use-case traceability, private-media access matrix and outbox activation/rollback matrix govern this hardening migration. The README is the short overview; the bug archive retains the original eight-item review plus historical safeguards, not an exhaustive new readiness certification.

## Local implementation status (2026-11-20)

The source description and line references below retain the original static-review baseline, not the current working tree. Runtime guards/H1 and Wave 3 were implemented locally in earlier checkpoints. The remaining local implementation now includes Wave 4B transaction-owned producer/cohort cutovers, Wave 5 canonical action projection/operator recovery, Wave 6 request-independent submission extraction/bounded embedding, and Wave 7 guarded release tooling. All producer/claim flags still default OFF; historical transfer remains evidence-gated. See [README](../README.md) for final observed checks and [deployment tooling](DEPLOY_TOOLING.md) for configuration, rollback and external gates.

Independent bounded reviews found and drove corrections to pre-send database-failure classification, legacy media transfer, empty Drive snapshots, operator-deferred/exhausted retries, poll-loop resilience, recovery response parity and cache scope/fingerprint binding. Known-contract closure additionally fixes consent privacy, schema-error responses, finite numeric handling and real vehicle matching, with the observed integration/scoped results recorded in [README](../README.md#known-contract-closure-2026-11-21). Historical failing full-suite results are retained; targeted corrections do not retroactively make those runs green or establish deployed readiness.

No staging/production migration, live provider verification, producer activation, push or deployment is authorized by this status. Remove legacy compatibility only after the approved staging/production observation window proves no legacy writes/callers. Local implementation does not supply that operational evidence.

## Original reviewed implementation shape

This is a Python/FastAPI modular monolith with separate router, core, service, and worker packages. It should remain one application plus a separately supervised worker unless measured operational needs prove otherwise.

- `main.py`: app construction, shared HTTP/Redis/PostgreSQL resource setup, router registration, and shutdown.
- `routers/`: HTTP endpoints; `routers/survey.py` contains the dynamic form and survey submission flow.
- `core/`: process-level state, repositories, schema synchronization, and domain helpers.
- `services/`: image/form processing, AI extraction, Telegram and Google Drive integration, and other side effects.
- `workers/outbox_worker.py`: PostgreSQL outbox polling and periodic Drive synchronization routines.
- `schema.sql`: PostgreSQL tables and indexes; `data/forms/` and `static/ui/` hold form definitions and browser interfaces.

The submission route performs multipart/form handling, image checks, synchronous AI/image work, persistence, and several side-effect dispatches. Production-mode submissions are intended to persist to PostgreSQL. Test-mode submissions use local JSON storage. If PostgreSQL is unavailable at startup, the app currently continues with no pool; that behavior is a release-blocking data-loss defect, not an acceptable production fallback.

The submission transaction inserts an `outbox_events` record, but downstream actions are also sent to FastAPI `BackgroundTasks`. Those in-process tasks are not durable across process termination. The outbox worker's R2/Lark/Drive/Zalo branches currently log placeholder messages rather than performing those integrations. The repository Compose file defines only `app-dev` on an external network; it does not define a complete local database/Redis stack or an outbox worker. The deploy script names an outbox-worker container, but that alone does not prove how production currently supervises it.

### Required versus existing use-case baseline

| Flow | Observed implementation | Required disposition |
|---|---|---|
| Phone history | Pre-H1: anonymous phone endpoint/modal and contradictory public-200/privacy-401 tests | H1 approved and locally implemented: existing admin auth, translated maintenance, no public phone fetch, unchanged PII-free submission-ID status. Verified Telegram mapping never grants web access; see [H1 evidence](WAVE2_H1_PHONE_BOUNDARY.md). |
| Browser retry/interruption | The form generates a fresh key before every POST; the shielded crash helper inserts an interruption row independently | One durable attempt owner for normal/cancel/recovery writes and retained browser keys; known commit uncertainty does not authorize a second attempt or media deletion. |
| Vehicle change/warnings | `services/telegram_polling.py:725-743` only acknowledges both callbacks; vehicle binding/history operation exists separately | Repair required behavior in Wave 4. Preserve callback entry points, confirm candidate vehicle, commit real binding/history or warning intent/audit, and report delivery truthfully. These are not completed operations to preserve. |
| Manual approval | Status and notification update, but historical actions may still be SKIPPED because the first verdict was rejected/manual-review | Re-derive required actions from current status plus real provider/ledger evidence; enqueue once without overwriting completed/paid evidence. |
| Photos/repair/dossier/mirrors | Private `data/submission_photos` has multiple concrete readers and a Telegram replacement writer | Keep private root, shared durable mount and one manifest/revision adapter for ALL readers/writers. An ID is not read authority; do not move bytes into public `data/media`. |
| Telegram delivery | Admin review card AND configurable question/CTV verdict; distinct appeal/cancel/warning/support classes | H1 now enforces verified standalone mappings for question-source phone sends without driver/CTV rows; typed/legacy links cannot authorize delivery/history. Durable per-recipient work/receipts remain Wave 4. |
| Quotas | Web appeal 20/1h; Telegram appeal 5/2h; Telegram support 5/2h, with distinct counter scopes/semantics | Freeze the implementation plan's per-channel table for hardening; historical 3/8h is not authority to change policy. Keep active-state guards and correct misleading text. |
| Test mode | Explicit request/UI mode selects labelled DEV storage; the production authorization policy is not established by that flag | Record server-side form/environment/operator authority before rollout; preserve permitted isolated testing, not implicit DB fallback or production billing/customer exports. |
| Form/image system | Conditional/gated schema, multiple camera groups, slot uploads, image-object/check-engine editing and cross-worker refresh | Keep these real workflows, not a simplified single-receipt design; retain status polling, pre-check, cancel/repair, form lifecycle and customer/internal output distinctions in regression fixtures. |

H1 has explicit owner approval and scoped local verification in [WAVE2_H1_PHONE_BOUNDARY.md](WAVE2_H1_PHONE_BOUNDARY.md); it is no longer an unresolved feature choice. Production test-mode authority and rollout remain separate gates. Phone delivery permission settles neither those gates nor Wave 4 durability.

## Recommended architecture

Keep the existing deployment shape and dependencies. Improve boundaries inside the monolith rather than introducing network services.

1. **HTTP adapters** — routers authenticate and validate HTTP input, invoke an application use case, and map its result to the existing response contract. Keep transport concerns out of domain/persistence logic.
2. **Submission use case** — move orchestration out of `routers/survey.py`: schema validation, media verification, deterministic verdict, and durable submission recording. Keep expensive synchronous model/image operations off the event loop with bounded execution capacity.
3. **Persistence boundary** — use explicit repositories for submission, idempotency, action status, and outbox records. A successful production response requires a committed submission. Commit the submission and its downstream work records atomically. Keep Redis for disposable cache/rate-limit state, not as the only idempotency record.
4. **Durable side-effect processing** - PostgreSQL outbox is the single work owner after Wave 4 activation. Independently claimable per-effect/per-recipient records carry validated receipts, explicit policy-skip outcomes and visible terminal/uncertain states. Billing and deterministic object writes can be replay-safe; Telegram/Lark uncertain completion requires reconciliation or an explicit audited resend, not a blanket exactly-once promise. Submission bytes remain outside public mounts and all readers/replacements share the authorized manifest adapter.
5. **Runtime dependencies** — initialize pools and clients in app lifespan and inject them through explicit dependencies. Separate liveness from readiness; readiness must fail when required production dependencies are unavailable. Reject insecure production configuration at startup.

Do not change the public form schema, multipart field names, submission response shape, or admin/Telegram behavior without documenting compatibility and migration requirements.

## Original static-review bug register

Severity: **P0** blocks safe production operation; **P1** risks data confidentiality, integrity, or durable delivery; **P2** concurrency or performance hazard requiring planned correction.

### P0 — production can acknowledge a submission that was never persisted

**Evidence:** `main.py:116-131` catches PostgreSQL pool startup failure and continues with `state.db_pool = None`. `routers/survey.py:1881-2069` only writes production records inside `elif db_pool`; after that branch it proceeds and eventually returns HTTP 200 (`:2199`). `routers/system.py:33-35` reports healthy without dependency checks.

**Failure:** database unavailable at startup -> production submission follows no durable-write branch -> handler still returns a verdict and submission ID.

**Plan:** production lifespan fails on required database startup failure; after startup, `/ready` becomes 503 on database/schema loss and production submissions fail closed. Keep `/health` liveness-only. DEV mode requires the recorded server-side authority and stays explicitly isolated.

**Acceptance:** separately test lifespan failure and later database loss; a production submission cannot return persisted success without a committed row. Use real PostgreSQL for restored-path commit proof, not a mock-only claim.

### P1 — unauthenticated phone-history lookup exposes personal submission details

**Baseline evidence (before H1):** `get_survey_history` accepted phones without auth and the public modal fetched records. Public-200 contradicted privacy-401 tests; rate limiting and phone knowledge were not ownership proof.

**Prior failure:** an anonymous caller could retrieve submission IDs, verdicts and rejection details by phone.

**Implemented H1 boundary:** `verify_admin_auth` executes before phone validation/storage/rate effects. Operators retain their response; the public modal has bilingual maintenance and no phone query. Trusted standalone Telegram bindings authorize delivery/private bot history ONLY. Self-contact IDs must match; command/legacy/getChat sources cannot send, read history or overwrite trust. Contact verification no longer requires driver/CTV records.

**Locally verified, not deployed:** focused HTTP/bot/mapping checks, real-PostgreSQL trust/overwrite/dispatch proof and actual Chromium maintenance/status checks passed as recorded in [WAVE2_H1_PHONE_BOUNDARY.md](WAVE2_H1_PHONE_BOUNDARY.md). The superseded public-access assertion was intentionally updated to 401; no privacy assertion was weakened. Verified web self-service remains deferred.

**Ownership correction:** trusted-source tags cannot replace another verified account. Disputed numbers are paused with their original owner preserved until explicit expected-owner revocation/re-verification. Same-account older bindings are intentionally retained; bot history and original verdicts cannot cross the verification-time boundary after a phone transfer. `/ready` includes the mapping relation and CI selects its real-PostgreSQL regressions. See the final correction evidence in [WAVE2_H1_PHONE_BOUNDARY.md](WAVE2_H1_PHONE_BOUNDARY.md); no authenticated mapping-management UI or production deployment is claimed.

### P1 — outbox does not durably execute the downstream actions it claims to protect

**Evidence:** `routers/survey.py:2031-2058` writes a single `flow_target="ALL"` event in the submission transaction, while `:2072-2094` schedules `dispatch_action_checklist` as an in-process FastAPI background task. `services/action_checklist.py:257-370` performs R2/Lark/Drive actions from that task. In `workers/outbox_worker.py:90-104`, R2/Drive/Lark/Zalo branches only log; the `ALL` approval event follows the billing `if` branch and is marked processed at `:107-115`.

**Failure:** process termination can discard the in-process actions; replaying the outbox event does not perform those integrations, and the worker can mark it processed after placeholder behavior.

**Plan:** commit required per-action work atomically, including both submission notification classes, appeals/cancel, moderation, authorized repair and manual Drive jobs. Reuse integrations but upgrade their return contracts for structured receipt/uncertainty handling. Keep four outbox statuses: policy SKIPPED is a PROCESSED result outcome, not a fifth status. Deploy schema/compatible workers before activating any new target.

**Acceptance:** app death after commit does not lose work intent; independent failures do not block unrelated actions. Real provider/database receipts or explicit policy skips justify PROCESSED. A post-send/pre-ack crash produces reconciled success or visible DELIVERY_UNCERTAIN, not fabricated success or blind duplicate sends. Test per-recipient partial success and audited resend behavior.

### P1 — failed extended PostgreSQL insert leaves the transaction aborted before fallback

**Evidence:** `routers/survey.py:1988-2005` catches any extended insert exception and issues a minimal fallback insert in the same connection transaction, with no savepoint or rollback to a savepoint.

**Failure:** PostgreSQL rejects the first statement -> transaction enters aborted state -> fallback statement cannot execute -> persistence fails despite the fallback path.

**Plan:** use a savepoint around the optional extended insert and roll back to it before fallback, or replace the broad fallback with explicit, separately tested compatibility handling.

**Acceptance:** simulate a server-side extended-insert error and prove the fallback can commit the minimal record without losing the surrounding transaction's intended writes.

### P1 — idempotency misses inferred-phone retries and permits concurrent duplicates

**Evidence:** `routers/survey.py:1184-1206` looks up before inferred phone (`:1256-1257`) and stores under a different identity (`:2189-2198`); caches do not atomically claim. `static/ui/dynamic_form.html:3492-3493` rotates the key per POST. `_persist_interrupted_submission`/`_submission_crash_guard` (`routers/survey.py:1066-1126`) are independent recovery writers.

**Failure:** retries/concurrent requests can create multiple submissions, and recovery can commit a row without an idempotency record. The superseded plan's unconditional failed-transaction cleanup WOULD delete its evidence; this is a proposed cleanup regression, not a claim that such cleanup currently runs.

**Plan:** canonical scope/key/fingerprint, native-crypto browser attempt retention, and ONE transaction owner for normal/recovery submission, response and wave-appropriate work. RESERVED is uncommitted internal state, not a separately visible lease. Wave 3 retains legacy ALL; new targets wait for 4A/4B. Preserve no-key API compatibility without promising cross-request dedupe.

**Acceptance:** browser lost-response retry, independent database connections, explicit/inferred phone, cancellation, evaluation failure and uncertain commit all resolve to one committed attempt or an explicit unavailable/uncertain outcome. Changed fingerprint returns 409 without leaking a stored response; no orphan cleanup during uncertainty.

### P1 — production settings accept unsafe built-in credential/secret defaults

**Evidence:** `config.py:13-17` provides built-in admin credentials and an application secret; settings construction at `:66-69` does not show production validation.

**Failure:** deployment omitting required environment values can start using known development defaults.

**Plan:** require explicit recognized environment, reject missing/default production credentials and short/non-random secrets, and preserve explicitly injected development/test settings. Omitted ENVIRONMENT must not silently select development to bypass validation. Never log secret values; optional providers are validated only when enabled.

**Acceptance:** production configuration tests fail closed for missing/default secrets and pass with injected high-entropy secrets. Do not log the secret values.

### P2 — action lease acquisition is a check-then-write race

**Evidence:** `services/action_checklist.py:99-120` reads current action status and checks its lease; `:150-167` writes the new lease in a separate operation without a conditional claim predicate or lock.

**Failure:** two workers can read the same pending state and both write a lease, then both execute a non-idempotent side effect.

**Plan:** Wave 4 claims atomically with a token/expiry and fences EVERY success, retry, failure, renewal and projection write. Wave 5 only removes legacy writers after their caller inventory is empty; it is not a deferral of claim correctness.

**Acceptance:** one concurrent winner, bounded attempts/backoff, stale success/failure cannot overwrite a new owner, and completed actions cannot be reclaimed. Expired replay-safe work can resume; an uncertain non-idempotent send is reconciled/quarantined rather than blindly re-sent.

### P2 — synchronous embedding work runs inside the async submission handler

**Evidence:** direct embeddings run at `routers/survey.py:1660,1911`; synchronous intra-submission duplicate checking at `:1639` computes embeddings internally (`services/duplicate_detector.py:155`). Offloading only the final insert-time call does not fix the whole path.

**Failure:** CPU-bound inference can block the event loop and delay unrelated requests at concurrency.

**Plan:** one bounded lifespan executor/admission policy for ALL heavy embedding paths, with shutdown and reuse of valid computed embeddings. Initial bound is one running plus one waiting task; saturation returns controlled retry, not an invented verdict.

**Acceptance:** slow intra-, inter-submission and persistence inference all permit unrelated lightweight requests to progress, with bounded execution/queue/memory and correct shutdown. No performance target is claimed without measurement.

## P0 rollout gates added by the reconciled audit

| Gate / classification | Evidence | Required correction and closure proof |
|---|---|---|
| Private-media authorization: PROPOSED regression prevented by corrected documentation, not an observed exposure incident | The superseded plan proposed `data/media/submissions`; `main.py:190-191` publicly mounts data/media. Existing admin photo endpoints require auth. | Corrected Wave 4 keeps private data/submission_photos and freezes each reader/writer's authority. Before runtime cutover prove anonymous known-ID/traversal denial, all-slot/replacement parity and shared durable access. Documentation correction is not runtime verification. |
| CI mutable-data preservation: EXISTING checked-in destructive path, not proven production loss | `.github/workflows/pipeline-ci-cd.yml:72-90,116-134` uses rsync --delete without excluding data, restarts only app, probes health | Gate automatic deployment before source pushes can trigger it; protect mutable data/credentials, apply/check migrations, deploy/verify compatible worker and readiness. Use a disposable deployment-retention fixture and operator-approved staging rollback rehearsal. Do not test deletion or contention in production. |

### Delivery-helper precision

`send_telegram_notification` (`routers/survey.py:2424-2445`) exposes no outcome and only warns for HTTP 429. `send_telegram_direct_message` (`:2543-2596`) returns Boolean, retries one 429 and logs message_id. Its dispatcher (`:2740-2764`) checks the Boolean, tries the alternate bot and logs final failure. It does NOT persist/propagate a final receipt. Lark only returns Boolean and logs record_id. Neither 'all helpers swallow failures' nor 'all callers ignore results' is accurate; the missing contract is a durable per-recipient success reference/acknowledgement and uncertainty outcome.

### Schema and release contract gaps

There is no automatic PostgreSQL application path merely from editing/copying schema.sql; core/schema_sync is form JSON. The corrected plan requires versioned 001/002 SQL, checksum ledger, actual psql invocation and real-PostgreSQL integration gates. TELEGRAM_NOTIFY is planned schema+handler work, activated only after a compatible worker, not evidence that today's absence alone is a new defect. Legacy ALL ownership, skip projection and each wave's valid rollback state are explicit. The old RESERVED wait branch must not motivate splitting atomic transactions without a complete new lease/recovery protocol.

## Suggested implementation-plan order

1. Gate unsafe automatic release paths; establish isolated baseline/real PostgreSQL tests. Documentation-only changes do not themselves disable current CI.
2. Close production fail-open/default configuration and stage history auth plus H1-approved UI; keep test-mode authority explicit.
3. Apply 001, then implement browser/normal/recovery atomic attempts with legacy ALL as the honest intermediate state.
4. Apply 002 and compatible workers with producers off (4A); transfer every required producer, private media reader/writer and moderation repair with receipt/reconciliation proof (4B).
5. Remove legacy lease writers, then extract orchestration and bound all CPU inference; retain corrected contracts throughout. Complete operational closure only after the activation/rollback and use-case matrices pass.

## Handoff definition of done

The corrected implementation plan supplies affected owners, private-media access, per-channel quotas, schema commands, provider outcomes, use-case/test traceability and per-wave activation/rollback. Follow one wave at a time, preserve unrelated edits and stop at unresolved approval gates. Do not treat historical diagrams, SLAs, cost estimates or audit assertions as implementation proof. Reconfirm the base revision and record actual behavioral evidence before closing runtime findings. No tests, migrations or production checks were run for this documentation correction.
