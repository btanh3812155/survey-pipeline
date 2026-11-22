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
3. Back up selected prior code under `.release-backups/release-XXXXXXXX/`, record target identity, then pause app and worker. When migrations are pending, require `--apply-migrations` and a nonempty target database backup before applying every pending migration in checksum order (001–007).
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

Migration 001 retains the Wave 3 attempt/private-media contract. Migration 002 adds durable owner/decision revisions, target/action identities, token leases and receipts while retaining legacy rows/four statuses. Migration 003 adds operational heartbeat/build evidence. Migration 004 adds `image_dinov2_exceptions`, a time-bound per-form allowlist for the inter-submission fraud tiers; the detector requires a 384-d DINOv2 cosine match (`>= 0.99`) and ignores rows once `expires_at` passes, so revocation needs no cron. Migration 008 adds `admin_module_registry` plus a one-time seed marker for `/admin/modules`; object definitions are database-authoritative after startup, and logic modules may reference only implementations compiled in `services/logic_modules.py` — stored JSON never executes code. Migration 009 adds `drive_export_settings` and `drive_export_clients` for `/admin/exports`. `schema.sql` mirrors fresh-install structure; it is not an upgrade command. For an owned disposable target, the isolated helper applies each version explicitly:

```bash
bash scripts/apply_migration.sh scripts/migrations/001_submission_attempts.sql
bash scripts/apply_migration.sh scripts/migrations/002_outbox_delivery.sql
bash scripts/apply_migration.sh scripts/migrations/003_outbox_operations.sql
```

That helper requires an explicitly isolated `UPGRADE_TEST_DATABASE_URL` and, for its Docker path, owned container/database/endpoint identity. It never authorizes a production migration.

Workers have two execution slots, 60-second token leases renewed every 15 seconds, at most five claims, due-time backoff and target fairness. Every finish/renewal/projection transition is token-fenced. Telegram success requires bot/chat/message identity; ambiguous acceptance is `FAILED/DELIVERY_UNCERTAIN`, not automatic resend. Billing preserves an existing ledger/paid state. Disabled/unconfigured policies have explicit skip reasons. Private R2 uses a separately approved bucket and immutable hashes, never the public/local fallback. Drive publication uses the existing advisory exclusion and holds current approved-row locks during publication, trading moderation concurrency for stale-export prevention.

`python -m workers.outbox_worker --health` is read-only. It reports schema/capabilities, build/poll evidence and queue state; invoking it does not manufacture a heartbeat. `/health` is liveness; `/ready` requires writer schema and, when any durable producer is enabled, its delivery schema. Bounded embedding work is lifespan-owned: one execution slot plus one waiter; saturation returns 503 with Retry-After instead of a made-up verdict.

## Google Drive exports

`GET /admin/exports` is the operator UI for Drive export configuration (migration 009 required). Folder layout under `GOOGLE_DRIVE_ROOT_FOLDER_ID`:

```
<master folder>/
  Client Exports/
    <client folder>/<form>/<form> (Google Sheet)
    <client folder>/<form>/Images/<submission>/<shortcut>
  Internal Exports/
    <category>/<form>/<form> (Google Sheet)
    <category>/<form>/Images/<submission>/<shortcut>
  Images Master/
    <form>/<submission>/<image>
```

Each client or internal configuration selects a date range, forms, fields, statuses, vehicle kinds, and whether images are included. Empty form/vehicle selections mean all forms/kinds present in the filtered rows; empty fields use the safe default. Only real submissions are exported; DEV/TEST-style IDs are excluded. Output sheets are native Google Sheets named after the form and are overwritten on each run. Images are uploaded once to `Images Master`; every client/internal image folder contains Drive shortcuts to the master file, never duplicate uploads. The run acquires a PostgreSQL advisory lock so two admin clicks cannot overlap. It also removes archived-form folders, stale image files/shortcuts inside each submission folder, and client/internal submission folders no longer selected by the configuration. Form and submission deletion hooks remove their Drive folders best-effort. Google Sheets revision history remains provider-managed; the cleanup targets folders and deduplicated image files, not Sheet revisions. The page stores the last run report; no provider call is made unless an operator presses Run. Drive sharing permissions for each client folder remain a separate manual/provider operation; this page controls data selection, not Google ACLs. Shortcuts also require the viewer to have access to the target master file, so grant per-file or approved-group access where clients must open images. Run this only after the target's Drive credentials, root folder, migration 009, and client permissions are approved.

## Admin recovery mail and break glass

Apply checksum-guarded migration `007_admin_account_bin.sql` (requires `006`) before enabling recovery on an upgrade; `schema.sql` mirrors its tables for fresh installs. Migration 007 normalizes legacy `admin` roles to `reviewer` except the active, nondeleted legacy owner `admin`, which receives `superadmin` and `admin@trustbridgevn.com` if its address was empty. Re-running the migration does not re-promote a demoted owner or restore an old email. `ADMIN_RECOVERY_EMAIL` seeds a new empty database and supplies a default address for newly created/promoted superadmins with no address; it is not a reset or email-change override.

Provision these app-service settings using approved deployment secrets, not git or shell history:

| Setting | Required value |
|---|---|
| `SMTP_HOST` | Provider's authenticated SMTP submission hostname, matching its TLS certificate |
| `SMTP_PORT`, `SMTP_TLS_MODE` | `587` + `starttls` or `465` + `implicit`, as documented by the provider; no plaintext mode |
| `SMTP_USERNAME` | Provider's SMTP login (for Google Workspace, the complete sending mailbox address) |
| `SMTP_PASSWORD` | Provider-issued SMTP credential or app password, not the mailbox's interactive password |
| `SMTP_FROM_EMAIL` | Defaults to `admin@trustbridgevn.com`; the SMTP provider must authorize that mailbox or an approved alias as a sender |
| `APP_SECRET` | Same private random value of at least 32 characters across all web app replicas; rotation invalidates pending codes |
| `FORWARDED_ALLOW_IPS` | Only trusted reverse-proxy peer IPs, never `*`; the app should not be directly internet-accessible |

For Google Workspace SMTP, use `smtp.gmail.com` with the settings above. The sending mailbox must have 2-Step Verification enabled, and account/organization policy must allow creation of an App Password. Use that generated App Password as `SMTP_PASSWORD`; a Drive OAuth token, service-account JSON or ordinary Google password does not qualify. If Workspace policy disallows App Passwords, obtain a different approved authenticated SMTP provider and its verified sender credentials. The app also needs outbound DNS and TCP access to the provider, a current CA trust store, and a trusted HTTPS reverse proxy supplying the client IP and original scheme. Provision separate staging credentials and approved test inboxes; code delivery has no fake fallback. Updating secret injection requires recreating the app service, not just copying code or restarting an existing container with unchanged environment.

`GET /admin/recovery` is public. Its request response is conditional to avoid revealing whether an account exists or a message was accepted. Missing SMTP configuration returns 503; when a configured provider fails, no code becomes usable and the app logs only the exception class. An authenticated `superadmin` uses `/admin/recovery/email` and must confirm separate codes sent to both the old and new addresses; a failed send to either returns 503 and invalidates the pending challenge. Codes expire after ten minutes and are limited to five guesses; resends are spaced by one minute. Request and confirmation limits are shared across replicas in PostgreSQL. Do not enable mail until the approved mailbox, sender and egress are provisioned and the recipient is verified out of band.

### SSH + database break glass (password only)

If mail is unavailable or inaccessible, obtain incident approval and SSH to the explicitly selected, pinned-host target. Verify the app/container and database identities and take an approved backup first. On that host, from the approved release environment (with `RELEASE_APP` and `RELEASE_DB_NAME` set), run the following using an interactive TTY. It prompts without echo, hashes locally with the application's PBKDF2-SHA256 implementation, and atomically changes exactly one active, nondeleted superadmin and records an audit event. No plaintext password enters shell history, environment, SQL or logs. Record the change in the incident record and confirm a new HTTPS login afterward.

```bash
: "${RELEASE_APP:?select the approved app container}" "${RELEASE_DB_NAME:?select the approved database}"
docker exec -it "$RELEASE_APP" python -c '
import getpass, os, sys, warnings
import psycopg
from core.admin_recovery import valid_password
from core.passwords import hash_password

if not sys.stdin.isatty():
    sys.exit("Interactive TTY required")
warnings.simplefilter("error", getpass.GetPassWarning)
username = input("Superadmin username: ").strip()
password = getpass.getpass("New password: ")
if not valid_password(password) or password != getpass.getpass("Confirm password: "):
    sys.exit("Passwords must match and contain 12–1024 printable ASCII characters")
encoded = hash_password(password)
del password
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    if conn.execute("SELECT current_database()").fetchone()[0] != sys.argv[1]:
        sys.exit("Wrong database; no change made")
    with conn.transaction():
        updated = conn.execute("""
            UPDATE admin_accounts SET password_hash=%s, updated_at=clock_timestamp()
            WHERE username=%s AND role=%s AND is_active AND deleted_at IS NULL
            RETURNING username
        """, (encoded, username, "superadmin")).fetchone()
        if updated is None:
            sys.exit("No eligible account; no change made")
        conn.execute("""
            INSERT INTO system_audit_log
                (component_name,action,entity_type,entity_id,actor_type,actor_id,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
        """, ("admin_recovery", "ADMIN_PASSWORD_BREAK_GLASS", "ADMIN", username,
              "SYSTEM", "ssh_db_operator", "{}"))
print("Password changed; older recovery challenges are invalid.")
' "$RELEASE_DB_NAME"
```

If the old recovery mailbox is permanently lost, keep email recovery unavailable until an approved, audited DBA correction verifies mailbox ownership and updates `recovery_email` with `updated_at`. Never change `ADMIN_PASSWORD`, `ADMIN_PASSWORD_HASH` or `ADMIN_RECOVERY_EMAIL` as a recovery procedure for an existing database account. There is no HTTP or environment-variable recovery backdoor.

## Acceptance evidence and external gates

Local acceptance exercises real disposable PostgreSQL/pgvector, intercepted provider protocols, actual HTTP adapters, bounded inference/event-loop progress, Chromium retry/media behavior and local fake-SSH/Docker release/rollback fixtures. Exact final results are recorded in the repository implementation-status section after verification. Provider mocks and filesystem rehearsals are not live provider or deployed-supervision evidence.

`dev` is the staging release branch; `master` is production. A push runs CI without a release. Before any manual staging dispatch, verify that the exact ref carries the guarded workflow, that the GitHub `staging` environment admits only `dev`, and that its variables and pinned, staging-scoped SSH credentials are configured. A branch rule is not reviewer approval. The guarded local command can also release the CI-tested checkout to an explicitly approved staging target. Never dispatch `master` for staging.

Staging bootstrap must precede the first guarded release. The legacy `survey-dev-app-1` at `/root/survey-pipeline-dev` uses `surveydb_dev` on a shared PostgreSQL container. Verify database-role and provider isolation before enabling a worker or migrating. During an approved staging maintenance window, provision the supervised worker and recreate the app with the restart policy, mounts and explicit build identity above. Do not reuse production credentials or select the production database.

Bootstrap result for `2eff9287f181ce85d831cae25cc7f21663bfed6f` (2026-11-21, `dev` downtime approved): `/root/survey-pipeline-dev` now runs `survey-dev-app-1` plus `survey-dev-outbox-v2-1`, both `unless-stopped`, `WorkingDir=/app`, `APP_BUILD_ID` equal to the release and `OUTBOX_V2_WORKER_ENABLED=true` with `DRIVE_SYNC_INTERVAL_SEC=0`; the app keeps `restart: always` only in the pre-bootstrap copy. Pre-bootstrap state was captured to `/root/backups/survey-dev-runtime-20261121.tar.gz` (`.env`, compose, `token.json`, 144 MB `data`) and `/root/backups/surveydb_dev-20261121.dump`; the release itself wrote `release-jwzf3MQy`. Ledger checksums are `001 9ffcaa4891d3`, `002 b02f2fc931a6`, `003 cfb91eff427a`. `/health` and `/ready` return 200 on `https://dev.146.190.201.32.sslip.io`; worker heartbeat age was 0.05 s at deploy and the build ID matched. A code-only rollback (`--rollback release-jwzf3MQy`) restored the prior files and left both services paused; the forward release `release-bUPthEgT` restored service. The pre-bootstrap legacy worker, started once to attach container networks, consumed the single pending legacy `ALL` event as a no-op (no collaborator, `result={}`), so that row is now `PROCESSED`. Staging still shares Telegram, Drive and AI provider credentials and destinations with production; R2 has no account id or keys in either environment, so public and private object exports are disabled and durable private exports record `POLICY_SKIPPED`. Staging-scoped credentials and the exercised zero-legacy-write canary remain open. Follow-up release `fc2bd0d7040cfc0c48bb456a85f658ed40429d13` (`release-6TihZzPZ`) points staging Drive delivery at the dedicated `survey-pipeline-dev` folder `1V-mk9dpCsvjtzhLjk7Xmm-acTQrZeSXp` and disables Lark: blank credentials plus an enqueue-time policy gate so an unconfigured Lark target records `INELIGIBLE_OR_UNCONFIGURED` instead of failing at dispatch. Production Lark behavior is unchanged pending an explicit production change.

Keep production activation separate. For an approved staging canary, record the build, migration checksums, backup ID, worker heartbeat, app readiness and code-only rollback result. Then observe a named durable cohort over a stated time window with representative traffic: check for new legacy `ALL`/unkeyed events, committed legacy checklist writes, and legacy provider dispatch, using evidence that covers all three. A zero queue snapshot, idle traffic or missing telemetry is not evidence of zero writes. Require complete producer/caller inventory and a global zero-write window before removing legacy compatibility.

Staging canary result (2026-11-21, observation window opened 05:33 UTC): a non-test `submit-webrtc` submission through `https://dev.146.190.201.32.sslip.io` for unregistered `canary_durable_v1` committed `VN-35A702E12E170E89` with `work_owner=outbox_v2`, six keyed events, zero `ALL`/unkeyed rows, and no legacy checklist `attempts`/`started_at`. The worker claimed and finished all six: four `POLICY_SKIPPED/NOT_APPROVED`, one `POLICY_SKIPPED/UNLINKED_RECIPIENT`, and one `TELEGRAM_NOTIFY/SUCCEEDED` with a provider receipt (`bot_id 9009195811`, `chat_id -1004391703479`, `message_id 4`). The window closed after 10m13s with the same six `PROCESSED` rows, zero failed/uncertain, no additional unkeyed rows, and an unchanged checklist projection. `legacy_unreconciled=1` is the negative control's `ALL` row, left untouched by design. The negative control `VN-3BDC87B504D824CE` ran before the compose fix with `work_owner=legacy` and checklist `attempts`/`started_at`, which is what the canary must exclude. Root cause of the control: `docker-compose.yml` did not forward any `DURABLE_*` flag, so `.env` toggles were inert; commit `f2c37a23` adds all five flags with `false` defaults. The dev admin bot is `@TrustBridgeAdminDev_bot` in supergroup `-1004391703479`; the user-side bot is the CTV bot `@TrustBridgeDev_bot`. The production rollout is recorded below.

Production rollout (2026-11-21, build `85aeda3e07e5fc009900539799080b86ef5a439`): `survey-pipeline-app-1` and `survey-pipeline-outbox-worker-1` now use `ENVIRONMENT=production`, `unless-stopped`, `APP_BUILD_ID=85aeda3…`, `/app`, `OUTBOX_WORKER_ID=survey-prod-outbox-v2`, claims enabled and the periodic Drive loop off; the five `DURABLE_*` producer flags stay `false`. `surveydb` received migrations 001-003 (`001 9ffcaa4891d3`, `002 b02f2fc931a6`, `003 cfb91eff427a`) after pre-migration backup `surveydb-prod-20261121.dump` and release `release-7DlkjoA6`; the successful release is `release-A76HuL6c`. Production also needed a real `APP_SECRET` because the validator rejects the built-in pepper. No pepper-dependent rewrite was necessary: `driver_identity_vault` is keyed by raw `driver_phone` with `ON CONFLICT (driver_phone)`, and `submission_idempotency` was empty. `TELEGRAM_ADMIN_GROUP_ID` now points at the migrated supergroup `-1004391703479` (old `-5445563473` is retired; the bot is an administrator in the new one), and Lark credentials are blank. `/health` and `/ready` return 200 on `https://survey.trustbridgevn.com`. Legacy `ALL` rows are now left for reconciliation by design, so `legacy_unreconciled` will grow while legacy producers remain active; transfer producers before removing compatibility. R2 remains unconfigured, and the dev admin/CTV bots are staging-only.

Temporary image exception (2026-11-21, build `3b2ae127bb09de2ea662ee76f03881612b48240b`, migrations 004-005): the seven attached income screenshots are registered in `image_dinov2_exceptions` for `TB_GSM_2026_Nov_Test`, expiring `2026-11-22T17:00:00Z` (2026-11-23 00:00 Asia/Ho_Chi_Minh). The inter-submission income, dHash and DINOv2 tiers are skipped when the primary image matches an unexpired row by 384-d embedding cosine `>= 0.99` or dHash Hamming distance `<= 6`. The intra-submission check also skips visual and income-data pairs involving an exempt image. Migration 005 adds `image_dhash`; all seven rows are backfilled and the live branch was verified (`[IMAGE EXCEPTION]`, no duplicate). Rejected submissions from before this change are not retroactively approved; resubmit them. The prod image predated `onnxruntime`, so a layered image (`FROM survey-pipeline-app:latest` plus `onnxruntime>=1.20.0`) was built and the app recreated; DINOv2 now loads in production. A proper image rebuild from current `requirements.txt` with a `.dockerignore` remains.

Driver verdict delivery (2026-11-21): `0961281310` was bound to Telegram `8094247707` with source `contact_share`, which verdict delivery rejects. An admin upgrade to `admin_verified` preserved the original `updated_at` (2026-11-21 02:36 UTC), so the lookup validates for submissions at or after that time. `VN-630441852EDE7A05` was resent through the driver path (`message_id=349`). The form remains on `tg_response_source: "question"`; the CTV code remains unlinked and is not used for this form.

Production and staging rollout (2026-11-22, build `1d1e4862de1cc681fe6d50cd917ff781d32a608a`): `dev` was fast-forwarded from `d2007ac` to `1d1e486`; staging `survey-dev-app-1`/`survey-dev-outbox-v2-1` and production `survey-pipeline-app-1`/`survey-pipeline-outbox-worker-1` were recreated with that `APP_BUILD_ID`. Staging applied migrations 004-009 in `release-vFqYqCib`; production applied 007-009 in `release-VEzslNtx`; both ledgers now contain 001-009. Verified `/health` and `/ready` return 200, `/admin/accounts`, `/admin/modules`, and `/admin/exports` return 401 without credentials, and deployed `admin_nav.js` contains the hamburger plus Module and Exports links. Production outbox snapshot after smoke was 13 pending, 1 failed, 0 uncertain (pre-existing backlog). No recovery email, Drive export, or durable-producer activation was triggered. Browsers may need a hard refresh because the previous static nav script was cached.

Admin navigation layout fix (2026-11-22, build `6aeb7adc4b0eee3c7dd91233dc1cbb2388686fdf`, staging `release-hX8Gke3t`, production `release-YxlVk79p`): the shared nav is now flush to the viewport; page-shell padding moved into body containers, and the account page keeps a `main` inset. The injected script URL includes `APP_BUILD_ID`, so browsers fetch the new nav instead of the cached flat version. Verified live static assets contain the new padding rules and hamburger markup on both targets. Module manager rows were then changed to native collapsible dropdowns in build `75a0281891897d03ee6fb27c2b4e81e9beab5737` (staging `release-fVf95ZX0`, production `release-sVWLTwkk`); no migrations were pending.

Form module pipeline (2026-11-22, build `8b37907af4ead5f5173581a5e216d7955d213e29`, staging `release-q861RV0D`, production `release-hny9tOkP`): the form editor Options tab now has an ordered module builder with add/remove/reorder, question/output/module/literal input sources, named outputs, and reject-on-false. Execution supports allowlisted logic modules, check-engine wrappers, and all-of composite checks. Named outputs persist in `form_data.module_outputs` and the submission response; a configured false output rejects the submission. Local verification covers schema validation, logic/engine/composite execution, browser add/save, and a real test submission returning `completion: 100.0`. No migration or durable-producer activation was required.

Admin console security update (build `1d1e486`, migrations 006-009): every `/admin` HTML response gets shared role-aware navigation. Database-backed Basic authentication requires an active account row; missing accounts and unavailable storage never fall back to environment credentials. Roles `superadmin`, `manager`, `reviewer` and `viewer` receive route-specific read/write access; password changes remain available to every role. `/admin/accounts` provides audited role assignment, activation, soft-delete and restore through a 30-day bin, with self-change and concurrent last-superadmin guards. Admin POSTs require exact same-origin Origin/Referer, unsafe pages are non-cacheable and unframeable, and failed Basic credentials use shared Redis rate limits. Public email-OTP password recovery and two-address, superadmin-only recovery-email rotation are implemented after migration 007 and SMTP provisioning. No 2FA or 2FA dependency is added. Email delivery remains unverified; deployment is recorded above.

External admin-security gates: SMTP host/port/TLS credentials, sender authorization, outbound DNS/TCP, and confirmed access to both recovery addresses are not provisioned here. Until then, mail recovery returns unavailable; use the documented approved SSH/database break-glass runbook. The admin navigation is client-side convenience only; API route role checks are authoritative.

Notification policy (2026-11-21, build `54fea522d8d8edff69ea63bf36e9a3b4e9bbe6e5`): verdict delivery resolves any non-conflicted `phone_telegram_links` binding that existed at the submission's original timestamp, preferring `verified_contact`/`admin_verified`. Unverified recipients receive the verdict plus a short system explanation, a "no action required if this is not your number" note, and a verification reply keyboard. Verification remains required for further actions (history, appeal, cancel, repair); the bot prompts when an unverified account attempts one. The durable producer mirrors the same lookup and notice. A phone with no binding cannot be messaged by the bot; the number owner must have started the bot (shared contact or `/linkphone`) before the verdict. CTV quotas are goals: reports use "Mục tiêu", show progress toward the goal, and never block a submission. Self-test `VN-TEST-UNVERIFIED` delivered the unverified notice (`message_id=350`).

Telegram webhook secret (2026-11-21): production `TELEGRAM_WEBHOOK_SECRET` was empty, so `verify_telegram_webhook_secret` returned `503` for every admin and driver webhook update; queued callback clicks never reached their handlers (the "view form" button produced no dossier). Generated a 43-character secret in the root-only prod `.env`, recreated `survey-pipeline-app-1`, and called `POST /api/v1/telegram/set-webhooks`; both bots returned `Webhook was set`, with `pending_update_count=0` and no last error. Re-registering drops Telegram's queued update, so a fresh click is required. Submission notifications themselves were already delivered; the newest submission `VN-47599082246837DB` sent both admin and driver messages at 11:01 UTC.

Admin maintenance (2026-11-21, build `5df0fe457f793a85b15aa13a27da06b54838721a`): while R2 is unconfigured, the legacy checklist records `SKIPPED/R2_NOT_CONFIGURED` at debug level instead of `FAILED`, and the submissions viewer hides R2 entries using `r2_enabled=false` from `/api/v1/submissions`. Each production row has a confirmed `Xóa` action that calls `DELETE /api/v1/submissions/{id}`; the server deletes the submission, its outbox events/delivery attempts, idempotency rows and media, then writes `SUBMISSION_DELETED` to `system_audit_log` with the admin actor, counts and IP. A submission with a `collaborator_billing_ledger` row is refused with `409`; dev-only rows have no delete button. Admin web auth is HTTP Basic. Production verifies `ADMIN_PASSWORD_HASH` when set; `ADMIN_PASSWORD` is now blank and remains a development/transition fallback. `APP_SECRET` is a separate PII pepper, not the admin credential.

Admin password hashing (2026-11-21, build `3d9e6273e3e4b94ea2b2681d8f68f7286a4b902b`): production uses PBKDF2-HMAC-SHA256 with 600,000 iterations. Encoding is colon-separated — `pbkdf2_sha256:<iterations>:<salt_b64url>:<digest_b64url>` — because compose interpolation treats a `$` in `.env` values as a variable and shrank the earlier dollar-separated hash. The legacy `$` form is still accepted on read. Verification is constant-time and successful Basic credentials are cached for 5 minutes (only a SHA-256 of password+hash is stored). The production validator requires a valid hash or a non-default plaintext during transition. Verified live: existing password `200`, wrong password `401`, app `ADMIN_PASSWORD_HASH` length 87, plaintext length 0, worker healthy.

Still external: durable producer activation on production (all five flags remain false), the legacy `ALL` reconciliation/transfer plan, a representative-traffic staging observation window beyond the single canary submission, Cloudflare dev bucket credentials, production test-mode authority, live-provider acceptance beyond the staging admin send, and optional Python Playwright coverage. No local completion entry authorizes automatic legacy cleanup or durable producer activation.
