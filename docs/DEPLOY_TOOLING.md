# deploy tooling

## Local implementation versus activation

Waves 4A–7 have local implementation and acceptance checks; this is not staging or production rollout evidence. Producer flags and worker claims default OFF. No flag alone transfers historical ownership. Follow the [implementation plan's activation matrix](MODULE_IMPLEMENTATION_PLANS.md#outboxschema-activation-and-rollback-matrix) and obtain explicit target/backup/activation approval before any remote operation.

The checked-in workflow deploys only on manual dispatch, after unit/PostgreSQL jobs and the selected GitHub environment's approval. It invokes the same guarded script as local operators; there is no second rsync implementation. Configure environment protection in GitHub separately: a YAML environment name does not create an approval rule.

## Release commands

```bash
scripts/deploy_prod.sh --target staging --schema 002 --dry-run --all-code
scripts/deploy_prod.sh --target staging --schema 002 --apply-migrations --all-code
scripts/smoke_prod.sh --target staging --schema 002
scripts/deploy_prod.sh --target staging --schema 002 --rollback release-XXXXXXXX
```

Examples describe the interface, not authorization to execute a deployment. `--dry-run` is local and makes no SSH connection. Other commands contact the explicitly selected target. `--schema 002` selects queue protocol 002 plus required operations migration 003. Old `--all-changed`, `--restart`, `--no-restart`, `--skip-compile`, dual-root copying and URL-only smoke options are removed.

The Makefile exposes the same interface:

```bash
make deploy TARGET=staging SCHEMA=002 FILES=--all-code ARGS=--dry-run
make smoke TARGET=staging SCHEMA=002
make rollback TARGET=staging SCHEMA=002 RELEASE=release-XXXXXXXX
```

### Explicit target configuration

| Variable | Contract |
|---|---|
| `RELEASE_HOST` | Explicit SSH target with pinned known-host identity; no built-in host |
| `RELEASE_ROOT` | Absolute, non-symlink application bind-mount root |
| `RELEASE_APP`, `RELEASE_WORKER`, `RELEASE_DB_CONTAINER` | Distinct existing containers; inspected immutable IDs are used afterward |
| `RELEASE_DB_NAME` | Explicit database selected by both app and worker; inspected shared network/endpoint must match |
| `RELEASE_DB_USER` | Database role; defaults to `postgres` |
| `RELEASE_BUILD_ID` | Explicit approved build, not `local`; must match both configured `APP_BUILD_ID` values and actual worker poll evidence |
| `RELEASE_TIMEOUT` | Probe deadline in seconds; defaults to 90 |

Production selects `ENVIRONMENT=production`; staging selects `development`. Preprovision app/worker with `/app` working directory, `unless-stopped` restart policies, matching database configuration and the same writable private `data` bind mount. The worker must run `python -m workers.outbox_worker` directly, have a stable explicit `OUTBOX_WORKER_ID`, `OUTBOX_V2_WORKER_ENABLED=true`, and `DRIVE_SYNC_INTERVAL_SEC=0`. Selected code paths must actually be bind-mounted into every consuming process. Unexpected nested data mounts or routing overrides fail preflight. Private R2 destination/account configuration must agree across app and worker.

The checked-in `app-dev`/opt-in `outbox-v2` Compose file is a development description, not this production topology. It depends on an existing image and external network, and does not create PostgreSQL/Redis. Provision approved secrets, provider policies/private destinations and supervised production services out of band. Release scripts do not rewrite configuration or turn on producers.

### Guarded sequence

1. Select only explicit allowlisted code files or tracked `--all-code` files; validate path containment, reject symlinks and hash every file. Newly added, untracked modules must be explicitly selected until committed. Never select runtime trees.
2. On target, take the release lock and verify actual container/environment/build/database/private-mount identity before mutation. Validate migration ledger/checksums and compile staged Python without executing it.
3. Back up selected prior code under `.release-backups/release-XXXXXXXX/`, record target identity, then pause app and worker. When migrations are pending, require `--apply-migrations` and a nonempty target database backup before applying 001, 002 and 003 in order.
4. Verify ledger/schema, atomically replace selected code and check installed hashes. No recursive runtime-root synchronization or deletion occurs.
5. Start worker first. Require protocol/operations versions, supported targets, expected build/worker identity and a finite, nonnegative, fresh heartbeat recorded after this process started. Start app only after that proof; verify `/health` and `/ready` and report queue ages/failures/uncertainty read-only.
6. Return `DEPLOY_RESULT` only after all gates pass. Enable an approved producer/cohort separately; inspect durable outcomes before expanding it.

Mutable forms, form backups, photos/revisions, DEV files, registries, Drive mirrors, models, credentials, tokens and infrastructure/configuration are outside the code allowlist. Smoke does not send submissions/provider messages, claim events, inject failures or mutate queue state.

### Failure and rollback

Failed publication/probes restore selected code when possible and leave app/worker paused; incomplete restoration is an explicit error. Manual rollback verifies the snapshot's target identity, restores only selected prior code and also leaves services paused. Keep additive schema, private media, receipts, delivery attempts, uncertainty, reconciliation mappings and audit evidence. Review retained configuration and compatibility before an approved restart. Never restore a placeholder worker or legacy dispatch for an `outbox_v2` record.

## Ownership controls

| Flag | New producer/cohort |
|---|---|
| `DURABLE_WEBRTC_ENABLED` | WebRTC submissions |
| `DURABLE_LARK_ENABLED` | Stable Lark-event side-effect intents; no invented survey row |
| `DURABLE_MODERATION_ENABLED` | Moderation, warnings and authorized repair transitions |
| `DURABLE_APPEALS_ENABLED` | Appeal/cancel transitions |
| `DURABLE_DRIVE_ENABLED` | Manual/periodic full-sync jobs |

All default false. A transferred record's committed `work_owner='outbox_v2'` remains authoritative after a flag is disabled: flag rollback cannot restart its legacy background effects. Legacy checklist compatibility is limited to a locked, never-started legacy record and cannot claim durable-owned rows. Keep it until the observation window proves zero legacy callers/writes; local tests do not authorize removing that rollback boundary.

Before historical transfer, quiesce/drain the legacy producer and inspect provider/ledger evidence. Authenticated operator reconciliation records parent/child mappings; ambiguous legacy/RUNNING effects are quarantined, not blindly expanded. Status projection is not a second queue. Ordinary retry excludes completed work and ambiguous sends; explicit reconcile/resend actions require evidence or acknowledged duplicate risk, preserve attempt history and audit the operator.

## Schema and worker behavior

Migration 001 retains the Wave 3 attempt/private-media contract. Migration 002 adds durable owner/decision revisions, target/action identities, token leases and receipts while retaining legacy rows/four statuses. Migration 003 adds operational heartbeat/build evidence. `schema.sql` mirrors fresh-install structure; it is not an upgrade command. For an owned disposable target, the isolated helper applies each version explicitly:

```bash
bash scripts/apply_migration.sh scripts/migrations/001_submission_attempts.sql
bash scripts/apply_migration.sh scripts/migrations/002_outbox_delivery.sql
bash scripts/apply_migration.sh scripts/migrations/003_outbox_operations.sql
```

That helper requires an explicitly isolated `UPGRADE_TEST_DATABASE_URL` and, for its Docker path, owned container/database/endpoint identity. It never authorizes a production migration.

Workers have two execution slots, 60-second token leases renewed every 15 seconds, at most five claims, due-time backoff and target fairness. Every finish/renewal/projection transition is token-fenced. Telegram success requires bot/chat/message identity; ambiguous acceptance is `FAILED/DELIVERY_UNCERTAIN`, not automatic resend. Billing preserves an existing ledger/paid state. Disabled/unconfigured policies have explicit skip reasons. Private R2 uses a separately approved bucket and immutable hashes, never the public/local fallback. Drive publication uses the existing advisory exclusion and holds current approved-row locks during publication, trading moderation concurrency for stale-export prevention.

`python -m workers.outbox_worker --health` is read-only. It reports schema/capabilities, build/poll evidence and queue state; invoking it does not manufacture a heartbeat. `/health` is liveness; `/ready` requires writer schema and, when any durable producer is enabled, its delivery schema. Bounded embedding work is lifespan-owned: one execution slot plus one waiter; saturation returns 503 with Retry-After instead of a made-up verdict.

## Acceptance evidence and external gates

Local acceptance exercises real disposable PostgreSQL/pgvector, intercepted provider protocols, actual HTTP adapters, bounded inference/event-loop progress, Chromium retry/media behavior and local fake-SSH/Docker release/rollback fixtures. Exact final results are recorded in the repository implementation-status section after verification. Provider mocks and filesystem rehearsals are not live provider or deployed-supervision evidence.

`dev` is the staging release branch; `master` is production. A push runs CI without a release. Before any manual staging dispatch, verify that the exact ref carries the guarded workflow, that the GitHub `staging` environment admits only `dev`, and that its variables and pinned, staging-scoped SSH credentials are configured. A branch rule is not reviewer approval. The guarded local command can also release the CI-tested checkout to an explicitly approved staging target. Never dispatch `master` for staging.

Staging bootstrap must precede the first guarded release. The legacy `survey-dev-app-1` at `/root/survey-pipeline-dev` uses `surveydb_dev` on a shared PostgreSQL container. Verify database-role and provider isolation before enabling a worker or migrating. During an approved staging maintenance window, provision the supervised worker and recreate the app with the restart policy, mounts and explicit build identity above. Do not reuse production credentials or select the production database.

Bootstrap result for `2eff9287f181ce85d831cae25cc7f21663bfed6f` (2026-11-21, `dev` downtime approved): `/root/survey-pipeline-dev` now runs `survey-dev-app-1` plus `survey-dev-outbox-v2-1`, both `unless-stopped`, `WorkingDir=/app`, `APP_BUILD_ID` equal to the release and `OUTBOX_V2_WORKER_ENABLED=true` with `DRIVE_SYNC_INTERVAL_SEC=0`; the app keeps `restart: always` only in the pre-bootstrap copy. Pre-bootstrap state was captured to `/root/backups/survey-dev-runtime-20261121.tar.gz` (`.env`, compose, `token.json`, 144 MB `data`) and `/root/backups/surveydb_dev-20261121.dump`; the release itself wrote `release-jwzf3MQy`. Ledger checksums are `001 9ffcaa4891d3`, `002 b02f2fc931a6`, `003 cfb91eff427a`. `/health` and `/ready` return 200 on `https://dev.146.190.201.32.sslip.io`; worker heartbeat age was 0.05 s at deploy and the build ID matched. A code-only rollback (`--rollback release-jwzf3MQy`) restored the prior files and left both services paused; the forward release `release-bUPthEgT` restored service. The pre-bootstrap legacy worker, started once to attach container networks, consumed the single pending legacy `ALL` event as a no-op (no collaborator, `result={}`), so that row is now `PROCESSED`. Staging still shares Telegram, Drive and AI provider credentials and destinations with production; R2 has no account id or keys in either environment, so public and private object exports are disabled and durable private exports record `POLICY_SKIPPED`. Staging-scoped credentials and the exercised zero-legacy-write canary remain open. Follow-up release `fc2bd0d7040cfc0c48bb456a85f658ed40429d13` (`release-6TihZzPZ`) points staging Drive delivery at the dedicated `survey-pipeline-dev` folder `1V-mk9dpCsvjtzhLjk7Xmm-acTQrZeSXp` and disables Lark: blank credentials plus an enqueue-time policy gate so an unconfigured Lark target records `INELIGIBLE_OR_UNCONFIGURED` instead of failing at dispatch. Production Lark behavior is unchanged pending an explicit production change.

Keep production activation separate. For an approved staging canary, record the build, migration checksums, backup ID, worker heartbeat, app readiness and code-only rollback result. Then observe a named durable cohort over a stated time window with representative traffic: check for new legacy `ALL`/unkeyed events, committed legacy checklist writes, and legacy provider dispatch, using evidence that covers all three. A zero queue snapshot, idle traffic or missing telemetry is not evidence of zero writes. Require complete producer/caller inventory and a global zero-write window before removing legacy compatibility.

Staging canary result (2026-11-21, observation window opened 05:33 UTC): a non-test `submit-webrtc` submission through `https://dev.146.190.201.32.sslip.io` for unregistered `canary_durable_v1` committed `VN-35A702E12E170E89` with `work_owner=outbox_v2`, six keyed events, zero `ALL`/unkeyed rows, and no legacy checklist `attempts`/`started_at`. The worker claimed and finished all six: four `POLICY_SKIPPED/NOT_APPROVED`, one `POLICY_SKIPPED/UNLINKED_RECIPIENT`, and one `TELEGRAM_NOTIFY/SUCCEEDED` with a provider receipt (`bot_id 9009195811`, `chat_id -1004391703479`, `message_id 4`). The window closed after 10m13s with the same six `PROCESSED` rows, zero failed/uncertain, no additional unkeyed rows, and an unchanged checklist projection. `legacy_unreconciled=1` is the negative control's `ALL` row, left untouched by design. The negative control `VN-3BDC87B504D824CE` ran before the compose fix with `work_owner=legacy` and checklist `attempts`/`started_at`, which is what the canary must exclude. Root cause of the control: `docker-compose.yml` did not forward any `DURABLE_*` flag, so `.env` toggles were inert; commit `f2c37a23` adds all five flags with `false` defaults. The dev admin bot is `@TrustBridgeAdminDev_bot` in supergroup `-1004391703479`; the user-side bot is the CTV bot `@TrustBridgeDev_bot`. The production rollout is recorded below.

Production rollout (2026-11-21, build `85aeda3e07e5fc009900539799080b86ef5a439`): `survey-pipeline-app-1` and `survey-pipeline-outbox-worker-1` now use `ENVIRONMENT=production`, `unless-stopped`, `APP_BUILD_ID=85aeda3…`, `/app`, `OUTBOX_WORKER_ID=survey-prod-outbox-v2`, claims enabled and the periodic Drive loop off; the five `DURABLE_*` producer flags stay `false`. `surveydb` received migrations 001-003 (`001 9ffcaa4891d3`, `002 b02f2fc931a6`, `003 cfb91eff427a`) after pre-migration backup `surveydb-prod-20261121.dump` and release `release-7DlkjoA6`; the successful release is `release-A76HuL6c`. Production also needed a real `APP_SECRET` because the validator rejects the built-in pepper. No pepper-dependent rewrite was necessary: `driver_identity_vault` is keyed by raw `driver_phone` with `ON CONFLICT (driver_phone)`, and `submission_idempotency` was empty. `TELEGRAM_ADMIN_GROUP_ID` now points at the migrated supergroup `-1004391703479` (old `-5445563473` is retired; the bot is an administrator in the new one), and Lark credentials are blank. `/health` and `/ready` return 200 on `https://survey.trustbridgevn.com`. Legacy `ALL` rows are now left for reconciliation by design, so `legacy_unreconciled` will grow while legacy producers remain active; transfer producers before removing compatibility. R2 remains unconfigured, and the dev admin/CTV bots are staging-only.

Still external: durable producer activation on production (all five flags remain false), the legacy `ALL` reconciliation/transfer plan, a representative-traffic staging observation window beyond the single canary submission, Cloudflare dev bucket credentials, production test-mode authority, live-provider acceptance beyond the staging admin send, and optional Python Playwright coverage. No local completion entry authorizes automatic legacy cleanup or durable producer activation.
