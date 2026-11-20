# Survey Verification Pipeline

FastAPI-based survey intake and verification application with dynamic forms, image/AI checks, PostgreSQL persistence, Redis-backed runtime support, and Telegram/admin workflows.

## Current architecture

The repository is a modular monolith: one FastAPI application with `routers/`, `core/`, and `services/` packages, plus PostgreSQL outbox worker code. Keep planned changes within these boundaries unless measured operational needs justify a service split.

- `main.py`: application construction, shared client/pool setup, router registration, shutdown.
- `routers/`: HTTP endpoints; `routers/survey.py` owns the dynamic form and submission route today.
- `core/`: runtime state, repositories, schema synchronization, domain helpers.
- `services/`: form/image/AI processing and external integrations.
- `workers/outbox_worker.py`: PostgreSQL outbox and periodic Drive worker routines.
- `schema.sql`, `data/forms/`, `static/ui/`: database schema, form definitions, browser interfaces.

The code review found unresolved persistence, privacy, idempotency, delivery, moderation and deployment risks. The [module designer handoff](docs/MODULE_DESIGNER_HANDOFF.md) separates observed defects from required behavior and proposed-regression prevention. The [corrected implementation plan](docs/MODULE_IMPLEMENTATION_PLANS.md) supplies the migration contracts; a documentation correction is not evidence that runtime fixes or deployment checks passed.

No throughput, cost, or response-time target is claimed here. Establish those from repeatable measurements before using them as design constraints.

## Upgrade handoff status

Implement one independently executable wave using the plan's access, use-case/test, quota and activation/rollback matrices. H1 is approved and locally implemented: admin-only web phone history, public maintenance, and verified standalone phone mappings for question-source Telegram delivery/private bot history. See [the H1 boundary and evidence](docs/WAVE2_H1_PHONE_BOUNDARY.md). A verified phone link is not web authentication.

The checked-in workflow restricts deployment to manual dispatch, but manual synchronization still needs mutable-data protection and operator approval. H1 alone changed no deployment path and authorized no production access; pausing production test-mode authority and operator gates remain separate. Historical specifications do not override the corrected handoff.

Wave 3 is locally implemented, not deployed: migration `001_submission_attempts.sql` must be applied and verified before the writer is activated. `/ready` fails closed unless the migration ledger checksum, `submission_idempotency`, and the private-media columns are present. Start with an approved database backup, apply the additive migration with `scripts/apply_migration.sh`, then deploy a writer that requires it. Rollback means stop new writes and retain the additive schema; do not drop committed attempts or private media. Do not treat this repository state as production evidence.

## Local development

Run the test target with `make test` (uses `.venv/bin/python -m pytest tests -q`).

Wave 3 local acceptance: `make test-postgres` uses an owned disposable PostgreSQL/pgvector container and includes the submit-worker SIGKILL/retry regression. `node tests/test_wave3_browser.cjs` exercises real Chromium attempt/retry and encoder-race behavior with provider requests stubbed or blocked; it requires Node.js with `fs.globSync`, the Playwright package, and an installed Chromium build. These checks do not authorize staging/production access or activation.

The checked-in `docker-compose.yml` is not a self-contained stack: it declares `app-dev`, uses a prebuilt image, and attaches to the external `survey-pipeline_default` network. It does not create PostgreSQL, Redis, or an outbox worker. Configure dependencies explicitly before running the app; do not treat development fallback behavior as production persistence.

Copy `.env.example` only as a local-development starting point. Production must supply explicit credentials and secrets; see the [module designer handoff](docs/MODULE_DESIGNER_HANDOFF.md).

## HTTP entry points

- `POST /webhook/lark-survey`: Lark webhook ingress.
- `POST /webhook/telegram` and `/webhook/telegram/driver`: Telegram webhook handlers.
- `POST /api/v1/survey/submit-webrtc`: dynamic survey submission endpoint.
- `GET /health`: liveness-style response; it does not establish database readiness.

## Operational notes

Do not run production data synchronization from a local workstation. Verify the target deployment, credentials, and worker supervision before triggering a production sync.

Google Drive integrations exist in `services/google_drive_delivery.py`; the periodic loop is in `workers/outbox_worker.py`. The FastAPI lifespan does not start that worker, and the checked-in Compose file does not declare it. The outbox worker also contains placeholder logging for several downstream flows. See the handoff document before relying on those paths.

## Current documentation

- [Module designer handoff](docs/MODULE_DESIGNER_HANDOFF.md): current architecture review, active bugs, and acceptance criteria.
- [Wave 3 idempotency/media boundary](docs/WAVE3_IDEMPOTENCY_MEDIA_BOUNDARY.md): local implementation, migration activation and observed evidence.
- [Bug and safeguards archive](docs/BUGS_AND_SAFEGUARDS_ARCHIVE.md): active-review summary and historical safeguards.
- [Module implementation plans](docs/MODULE_IMPLEMENTATION_PLANS.md): corrected contracts, use-case/test and access matrices, versioned migration commands, recipient reconciliation, approval gates and per-wave activation/rollback; implementation evidence is still required.
- [Complete architecture manual](docs/PIPELINE_COMPLETE_ARCHITECTURE_MANUAL.md): historical design reference; verify implementation claims against code.
- [Operations and API reference](docs/PIPELINE_OPERATIONS_AND_API_REFERENCE.md): operational reference; verify deployment commands against the current environment.
