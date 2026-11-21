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

Still external: actual CI execution, approved staging parity/rollback and observation window, private destination/credential provisioning, production test-mode authority, verified target backups/topology/old-worker shutdown, live provider acceptance, and separately approved producer activation. No local completion entry authorizes production access, migration, release, push or automatic legacy cleanup.
