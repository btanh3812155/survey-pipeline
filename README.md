# Survey Verification Pipeline

FastAPI-based survey intake and verification application with dynamic forms, image/AI checks, PostgreSQL persistence, Redis-backed runtime support, and Telegram/admin workflows.

## Current architecture

The repository is a modular monolith: one FastAPI application with `routers/`, `core/`, and `services/` packages, plus PostgreSQL outbox worker code. Keep planned changes within these boundaries unless measured operational needs justify a service split.

- `main.py`: application construction, shared client/pool setup, router registration, shutdown.
- `routers/`: HTTP endpoints; `routers/survey.py` parses multipart/authentication and maps typed submission results to HTTP.
- `core/`: runtime state, bounded embedding executor, repositories, schema synchronization, durable intent/claim helpers.
- `services/`: `submission_pipeline.py` owns request-independent validation/evaluation/media/commit; provider handlers and action projections expose structured evidence.
- `workers/outbox_worker.py`: PostgreSQL outbox and periodic Drive worker routines.
- `schema.sql`, `data/forms/`, `static/ui/`: database schema, form definitions, browser interfaces.

The [module designer handoff](docs/MODULE_DESIGNER_HANDOFF.md) records the original risks and required behavior; the [implementation plan](docs/MODULE_IMPLEMENTATION_PLANS.md) records migration/acceptance contracts. Local implementation is not deployed-state evidence. Preserve their historical baselines and use the local status/evidence below rather than treating old implementation warnings as current source descriptions.

No throughput, cost, or response-time target is claimed here. Establish those from repeatable measurements before using them as design constraints.

## Upgrade handoff status

H1 is approved and locally implemented: admin-only web phone history, public maintenance, and verified standalone phone mappings for question-source Telegram delivery/private bot history. See [the H1 boundary and evidence](docs/WAVE2_H1_PHONE_BOUNDARY.md). A verified phone link is not web authentication. Production test-mode authority, CI execution, credentials, staging parity and operator rollout gates remain separate.

The workflow releases only on manual dispatch using the same guarded immutable-code script as operators. Target/database/build/mount identity, migration checksums, backup, worker post-start heartbeat, app readiness and mutable-state retention are release gates; failure/rollback leaves services paused. See [deployment tooling](docs/DEPLOY_TOOLING.md). GitHub environment approval rules and target provisioning still require external configuration.

Wave 3 is locally implemented, not deployed: migration `001_submission_attempts.sql` must be applied and verified before the writer is activated. `/ready` fails closed unless the migration ledger checksum, `submission_idempotency`, and private-media columns are present. `scripts/apply_migration.sh` is restricted to owned disposable test databases; an approved target rollout uses the guarded backup/migration/release procedure in [deployment tooling](docs/DEPLOY_TOOLING.md). Rollback stops new writes and retains additive schema, committed attempts and private media. This checkout is not production evidence.

Waves 4A/4B/5 are locally implemented with per-recipient/effect intents, token-fenced claims, truthful receipts/skips/uncertainty, audited retry/reconcile/resend and historical ownership quarantine. Migration `002_outbox_delivery.sql` retains legacy rows and adds owner/decision revisions; `003_outbox_operations.sql` adds build/poll evidence. `OUTBOX_V2_WORKER_ENABLED` and all five `DURABLE_*_ENABLED` producer flags default false. Existing `outbox_v2` ownership survives flag rollback; it never falls back to legacy dispatch. Legacy records require drain/evidence/reconciliation before transfer, not blind `ALL` replay.

Wave 6 extracts the WebRTC use case without FastAPI dependencies and bounds all embedding paths to one executing job plus one waiter. Saturation returns 503/Retry-After with retained media, not an invented verdict. Wave 7 supplies local release/rollback rehearsals and read-only operational probes. These source capabilities do not certify staging/production activation or authorize removing legacy compatibility before its observation window.

### Observed local acceptance (2026-11-20)

| Check | Observed result | Boundary |
|---|---|---|
| Full `pytest tests -q --tb=short`, run once | **787 passed, 75 failed, 2 skipped** | Not a green repository gate. Includes old expectations and compatibility-fixture failures; the exact historical/new split is not established. |
| Scoped post-fix HTTP/PG/worker/operator/provider/executor/release acceptance | **211 passed** | Runtime authorization, Wave 3 replay/savepoint/SIGKILL, 002 migration helper, durable WebRTC/Lark/appeal/moderation/Drive, reviewed retry/media/failure regressions, direct use case, event-loop bounds and release/rollback fixtures. Disposable PostgreSQL/pgvector; providers intercepted. |
| Registered-form/image/H1 compatibility fixtures | **58 passed, 1 deselected** | Actual service boundary and bounded lifespan; parallel AI classification/OCR contract retained. PostgreSQL-only H1 case excluded from this offline run. |
| Historical bot/notification compatibility run | **59 passed, 5 failed**, followed by scoped corrections | Superseded for `/audit`, `/forms`, and `/stats` by the later command and known-contract checks below. |
| Required-income OCR timeout and DEV moderation fallback corrections | **2 passed** | Failed OCR cannot count as audited income; non-production DEV fallback uses the actual cursor protocol. |
| `node tests/test_wave3_browser.cjs` | **9 passed** | Actual Chromium multipart retry/reload/changed-media/single-flight/encoder cancellation; provider traffic intercepted. |
| `bash -n` release/smoke/migration/PostgreSQL scripts | Passed | Shell syntax only; no target contacted. |

### Known-contract closure (2026-11-21)

Controlled schema-upload 422 responses, required/finite numeric validation, safe rejected-record serialization, consent refusal before storage/AI, read-only real vehicle checks and alias-safe writer-owned registration are implemented locally. Authentication-first Lark handling, idempotency conflicts, private data access and existing no-form API behavior were preserved. Stale mocks now target the actual service/lifespan boundaries; obsolete wording/SQL/route-shape assertions and the duplicate live-network geolocation test were removed rather than weakening security assertions. Telegram commands use the shared live form registry, and authorized `/stats` reports database unavailability rather than fabricated DEV counts.

| Acceptance stage | Observed result |
|---|---|
| Independent statistics/geolocation/layout/viewer/support | **19 passed** |
| Authentication/persistence slice | **71 passed, 1 failed**; invalid route-shape assertion corrected, then **4 passed** for HTTP access boundaries |
| Submission/form/Wave 3 regressions | **34 passed, 1 failed**; stale DEV-bypass refusal fixture corrected |
| Integrated `pytest tests -q --tb=short` | **891 passed, 6 failed, 1 skipped**; log `/tmp/survey-known-contract-closure.log` |
| Corrections for those six integration failures, plus registry/phone/command coverage | **41 passed, 1 failed**, then final transaction-fixture correction **1 passed** |

All six failures from that integrated run were addressed: private plate text expectations, explicit quota authority, faithful vehicle-read cursor results, authorized reply-to-dossier fixtures, the superseded inline-prompt test, and the stale Telegram registry import. Those overlapping scoped counts are historical and not additive. No unresolved policy decision remains in this closure scope. The original 75-failure run remains at `/tmp/survey-remaining-waves-final-pytest.log`.

### Fresh pre-push acceptance (2026-11-21)

Full `.venv/bin/python -m pytest tests -q --tb=short -ra`: **896 passed, 1 skipped, 1 warning** in 117.83 seconds, using an owned disposable PostgreSQL/pgvector database and offline provider settings. The skipped module is `tests/test_status_bar_browser.py` because Python Playwright is not installed locally; the warning is the existing oversized-image fixture's Pillow decompression-bomb warning. Exact output: `/tmp/survey-prepush-full-pytest.log`. The disposable database was stopped after the run. CI selects all PostgreSQL-marked tests rather than a hand-maintained subset, and both jobs explicitly use offline AI mode.

This closes the observed local full-suite failure gate, not the optional Python browser coverage or deployed readiness. CI execution, staging parity/rollback and approved production activation remain separate gates; no deployment or live provider verification occurred.

The clean-checkout CI tests authenticate Telegram webhook requests with a test-only secret, use synthetic camera uploads for OCR/blur regressions, and run migration and harness subprocesses with the same interpreter as pytest. The Drive mirror migration handles flat and per-form legacy folders even when their destination `submissions/` directory already exists.

Clean-checkout replay (no `.env`, private driver dataset, or checkout-local virtualenv): **830 passed, 67 skipped** in the unit job, including a clean placeholder gate; disposable PostgreSQL **76 passed, 1 skipped**, followed by **10** H1 and **126** runner/outbox passes. PostgreSQL and optional browser checks skipped by the unit job are separate from its unit-test pass count.

Concurrent outbox workers return a claim and record its attempt only when the conditional update acquires the lease. The disposable PostgreSQL regression verifies that two competing claimers produce one lease token and one attempt record.

## Local development

Run the test target with `make test` (uses `.venv/bin/python -m pytest tests -q`).

Start the web service with `.venv/bin/python -m core.server` (`WEB_CONCURRENCY` controls Uvicorn workers). This resolves exact `FORWARDED_ALLOW_IPS` addresses or Docker DNS peers before listening, then delegates forwarded-client handling to Uvicorn. Local defaults trust only loopback; the container/Compose defaults also resolve the `caddy` service. An unresolved configured peer or wildcard/network trust fails startup. Do not bypass this entrypoint with the old raw Uvicorn command when using DNS peer names.

Wave 3 local acceptance: `make test-postgres` uses an owned disposable PostgreSQL/pgvector container and includes the submit-worker SIGKILL/retry regression. `node tests/test_wave3_browser.cjs` exercises real Chromium attempt/retry and encoder-race behavior with provider requests stubbed or blocked; it requires Node.js with `fs.globSync`, the Playwright package, and an installed Chromium build. These checks do not authorize staging/production access or activation.

The checked-in `docker-compose.yml` is not a self-contained stack: `app-dev` and the opt-in `outbox-v2` profile use a prebuilt image and external `survey-pipeline_default` network. It does not create PostgreSQL or Redis. The outbox profile shares the private data volume, disables claims and periodic Drive sync by default, and supplies a separate schema healthcheck. Configure dependencies explicitly; do not treat development fallback behavior as production persistence.

Copy `.env.example` only as a local-development starting point. Production must supply explicit credentials and secrets; see the [module designer handoff](docs/MODULE_DESIGNER_HANDOFF.md).

## HTTP entry points

- `GET /`: survey/default host entry redirects to `/ctv`, then `/ctv/login` when signed out; operator subdomain routing and direct `/forms/{form_id}?ref=...` links remain available.
- `GET /ctv/login`, `POST /ctv/login/request`, `POST /ctv/login/verify`, `POST /ctv/logout`: CTV Telegram OTP login and logout.
- `GET /ctv`: authenticated personal information, quota-goal progress and currently open public surveys; referral-enabled forms receive the signed-in CTV's code, while non-attributed forms are explicitly marked.
- `POST /webhook/lark-survey`: Lark webhook ingress.
- `POST /webhook/telegram` and `/webhook/telegram/driver`: Telegram webhook handlers.
- `POST /api/v1/survey/submit-webrtc`: dynamic survey submission endpoint.
- `GET /health`: liveness-style response; it does not establish database readiness.

### CTV and persistent browser login

CTV login accepts either an active directory code from the dropdown or a normalized Vietnamese phone number. Both require a six-digit Telegram OTP; public referral codes never authenticate by themselves. The directory phone and positive Telegram user ID must match a standalone `verified_contact` or `admin_verified` binding. Unverified/conflicted mappings, non-CTV numbers and ambiguous directory identities cannot sign in. Existing private bot invitations and self-contact verification establish that binding first.

OTP challenges expire after five minutes, allow at most five attempts, are browser-bound and atomically single-use in Redis. Sending and verification are rate-limited. Delivery uses `TELEGRAM_DRIVER_BOT_TOKEN` and requires Telegram's successful response; no admin-bot fallback, fabricated OTP or process-local authentication fallback is used. PostgreSQL, Redis or Telegram outages fail closed.

Admin and CTV sessions use separate signed, host-only `HttpOnly`, `SameSite=Lax` cookies, `Secure` in production, retained for 30 days across browser restarts. Logout clears the corresponding cookie. Admin password changes/deactivation/deletion invalidate admin sessions. CTV sessions and pending OTPs bind to a durable directory `auth_revision` plus the verified phone binding; a database trigger rotates that revision on code, phone, Telegram ID or active-state changes, so disable/re-enable and identity-away/back cannot revive old credentials. Harmless name/notes edits preserve sessions. CTV authentication grants no admin or driver-history access. Personalized pages are not cached.

The dashboard reuses existing quota calculations, including mixed all-time/rolling goals. Quotas remain progress goals, not submission caps. It excludes private, archived and response-limit-closed forms; survey links stay on the current host. Forms disabling CTV referrals remain available without `ref`, with a visible warning that they do not count toward CTV progress. Migration `015_ctv_auth_revision.sql` adds the durable revision and security-change trigger; runtime CTV schema setup and `schema.sql` mirror it. Apply through the approved migration/release process before exposing the updated login. Source changes are not deployment evidence.

The admin CTV page uses horizontal SVG edit/delete actions with no quick-disable button or separate information column. Mobile rows expand from code-only summaries into full details and actions. Its CTV/form generator accepts active CTVs and open public forms with CTV tracking enabled, producing a same-origin link and PNG QR with a quiet zone. Link copy/share and QR image copy/share/download use native browser capabilities; unsupported or rejected clipboard/share operations report failure rather than success.

Local verification for these corrections: **193 focused pytest checks passed**, including actual disposable PostgreSQL/Redis revision lifecycle, OTP and guarded release/migration coverage. Actual Chromium desktop/mobile checks exercised row disclosures, edit/delete confirmation, link clipboard, PNG clipboard pixels, downloaded QR decoding/quiet zone, selection invalidation and updated CTV login. Native share API payloads/cancellation were intercepted for repeatable checking; an operating-system share sheet was not exercised. Shell syntax checks passed for release, smoke and migration helpers. No live providers or staging/production deployments were used.

## Operational notes

Do not run production data synchronization from a local workstation. Verify the target deployment, credentials, and worker supervision before triggering a production sync.

Google Drive integrations remain in `services/google_drive_delivery.py`; durable manual/periodic sync requests use independent `drive_sync` jobs and the existing advisory exclusion. The FastAPI lifespan does not start workers. Compatible workers require structured receipts or explicit skips; ambiguous non-idempotent effects are quarantined instead of blindly resent. Legacy compatibility remains fenced away from durable-owned records while untransferred cohorts/rollback still exist.

## Current documentation

- [Module designer handoff](docs/MODULE_DESIGNER_HANDOFF.md): current architecture review, active bugs, and acceptance criteria.
- [Wave 3 idempotency/media boundary](docs/WAVE3_IDEMPOTENCY_MEDIA_BOUNDARY.md): local implementation, migration activation and observed evidence.
- [Bug and safeguards archive](docs/BUGS_AND_SAFEGUARDS_ARCHIVE.md): active-review summary and historical safeguards.
- [Module implementation plans](docs/MODULE_IMPLEMENTATION_PLANS.md): migration contracts, use-case/test and access matrices, recipient reconciliation, approval gates and per-wave activation/rollback.
- [Deployment tooling](docs/DEPLOY_TOOLING.md): guarded script/workflow contract, ownership flags, retained rollback state and external activation gates.
- [Complete architecture manual](docs/PIPELINE_COMPLETE_ARCHITECTURE_MANUAL.md): historical design reference; verify implementation claims against code.
- [Operations and API reference](docs/PIPELINE_OPERATIONS_AND_API_REFERENCE.md): operational reference; verify deployment commands against the current environment.
