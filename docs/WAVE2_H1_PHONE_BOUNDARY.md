# Approved H1 and verified phone-to-Telegram boundary

## Scope and status

The owner approved admin-only web phone history, truthful public disablement, unchanged PII-free submission-ID status/polling, and standalone verified phone delivery. This document records the scoped local implementation and observed checks. No production access, deployment, commit, push, data backfill, OTP/SMS infrastructure, idempotency migration or outbox migration was performed.

The original full-suite baseline remains historical evidence in `WAVE0_BASELINE.md`; it was not rerun. Local acceptance of this boundary is not a full-suite or production-readiness claim. Durable delivery/uncertain provider outcomes remain Wave 4 work.

**Ownership correction:** the initial H1 upsert wrongly allowed any trusted-source account to replace a different trusted owner. That permission is removed. A proved conflicting claim preserves the recorded owner but pauses the disputed number as `ownership_conflict`; neither account receives delivery/history through that binding until explicit operator resolution. Initial verification results below are historical and did not cover this defect; the latest correction evidence is recorded at the end.

**Current Telegram history contract:** partner `/surveys` now requires a Telegram self-contact binding and uses the original saved notification-target phone, not `driver_phone`. This narrows the historical bot access described below. The verification-time cutoff and web history rules are unchanged. See the [current bot commands](PIPELINE_OPERATIONS_AND_API_REFERENCE.md#42-telegram-commands).

## Web history

- `GET /api/v1/surveys/history` uses the existing `verify_admin_auth` dependency before phone validation, storage access or rate-limit effects.
- Authorized operators retain the existing history response and DEV/PostgreSQL aggregation, including access to another phone in their operator scope.
- The existing 30-per-60-second abuse limit is keyed by authenticated principal plus source address, not the requested phone. Telegram verification is never a substitute for web authentication.
- The public modal retains its existing styling/close entry point but displays Vietnamese/English maintenance text. Phone/query controls and anonymous history fetching are removed. No admin credentials are added to public JavaScript.
- `GET /api/v1/survey/status/{submission_id}` and the browser status/polling implementation are unchanged; an isolated API regression and actual-browser check exercise them.

## Mapping and trust

`phone_telegram_links` remains independent of `authorized_users`, `ctv_directory` and the identity vault. No new table/column or migration is needed for this change: the existing source column supplies the trust classification.

| Source | Verdict delivery and bot history |
|---|---|
| `verified_contact` | Trusted only after the contact handler checks positive integer `contact.user_id == from_user.id == user_id` and successfully persists the mapping |
| `admin_verified` | Trusted only as an explicitly administrator-verified binding; this change adds no automatic admin verification or new management UI |
| `ownership_conflict` | Suspended, not trusted. Cannot deliver or authorize bot history; neither command claims nor repeated trusted-source claims can silently reactivate it. The recorded owner ID is preserved for explicit operator revocation. |
| `command`, `contact_share`, `getChat`, seeded/legacy/unknown/NULL or any other source | Unverified; cannot authorize phone-based verdict delivery or bot history |

Historical `contact_share` rows are intentionally not grandfathered: the old handler did not check self-contact identity. Existing users must share their own contact again, or an administrator must explicitly establish ownership through an approved process. Do not bulk-promote seed/getChat/command records.

- The handler rejects missing or third-party contact IDs before any mapping write. A failed persistence operation is not reported as successful verification.
- Question-source delivery normalizes the phone answer, reads only a trusted standalone mapping and sends to its positive Telegram account ID. It never treats raw digits or an arbitrary `@username` as a direct question-source chat target and does not fall back to driver/CTV records.
- An absent/unverified mapping skips the party verdict with a redacted diagnostic. The separate submission admin notification remains scheduled.
- `/linkphone` is an unverified claim and cannot replace, downgrade or suspend a trusted binding. Only a conflicting VERIFIED claim can pause a disputed number, and even `admin_verified` does not implicitly transfer it to a different account.
- Bot history requires a private chat and a trusted binding with an aware verification timestamp. SQL/DEV records before that timestamp, or DEV records with unknown/naive dates, are not exposed by current phone possession. Older history stays admin-only. SQL history rechecks the current binding/account/window, and a database read error fails closed instead of using a file fallback.
- Existing CTV-specific delivery and collaborator registration remain separate. Contact verification no longer creates or requires a driver/CTV/identity-vault row.

## Files

Runtime/UI: `core/phone_links.py`, `routers/survey.py`, `services/telegram_polling.py`, `static/ui/dynamic_form.html`.

Ownership correction additionally changes `routers/system.py`, `services/admin_moderator.py`, the PostgreSQL workflow invocation, `tests/test_runtime_dependencies.py`, and the original-timestamp assertion in `tests/test_user_facing_behavior.py`. No runtime dependency or SQL migration file was added.

Regression homes: `tests/test_phone_links.py`, `tests/test_party_notifications.py`, `tests/test_driver_bot.py`, `tests/test_history_privacy.py`, the history case in `tests/test_form_engine.py`, the existing privacy regression in `tests/test_privacy_and_tg_bot.py`, and `tests/test_status_bar_browser.py`. Existing response assertions are retained except the explicitly superseded public-history/direct-recipient permissions. Relevant fixtures now isolate storage/providers.

## Initial H1 verification (before the ownership correction)

### Focused HTTP, mapping, bot and delivery checks

Executed with explicit test-only settings, fake provider tokens, and dummy app database/Redis addresses (no app lifespan). The relevant providers/storage are mocked or temporary:

```bash
ENVIRONMENT=test ADMIN_USERNAME=h1-test-admin ADMIN_PASSWORD=h1-test-password \
APP_SECRET=h1_test_secret_only_0123456789abcdef \
DATABASE_URL=postgresql://test:test@127.0.0.1:1/h1_test \
REDIS_URL=redis://127.0.0.1:1/0 TELEGRAM_BOT_TOKEN=mock_tg_token \
TELEGRAM_ADMIN_BOT_TOKEN=mock_tg_token TELEGRAM_DRIVER_BOT_TOKEN=123:offline-test \
LARK_APP_ID=cli_mock_id LARK_APP_SECRET=mock_secret DEEPSEEK_API_KEY='' GEMINI_API_KEY='' \
.venv/bin/python -B -m pytest -q \
 tests/test_history_privacy.py tests/test_phone_links.py tests/test_party_notifications.py \
 tests/test_driver_bot.py::test_driver_bot_contact_sharing \
 tests/test_driver_bot.py::test_unverified_bot_history_never_reads_storage \
 tests/test_driver_bot.py::test_verified_bot_history_uses_only_bound_phone \
 tests/test_driver_bot.py::test_bot_history_is_not_disclosed_to_group_chat \
 tests/test_form_engine.py::test_survey_history_admin_access \
 tests/test_privacy_and_tg_bot.py::test_survey_history_unauthorized_returns_401 \
 tests/test_status_bar_browser.py
```

Observed: **43 passed, 2 skipped in 4.47s**. The skips were the real-PostgreSQL case without its DSN and the Python Playwright module unavailable in this virtual environment; both areas were separately exercised below. No baseline failures were suppressed or broadly reclassified.

### Real PostgreSQL trust and actual dispatch

Used the existing owned-container runner with remote targets disabled and selected only the NEW SQL/dispatch test, not the previously reported Wave 0 smoke tests:

```bash
ENVIRONMENT=test ADMIN_USERNAME=h1-test-admin ADMIN_PASSWORD=h1-test-password \
APP_SECRET=h1_test_secret_only_0123456789abcdef \
DATABASE_URL=postgresql://test:test@127.0.0.1:1/h1_test \
REDIS_URL=redis://127.0.0.1:1/0 TELEGRAM_BOT_TOKEN=mock_tg_token \
TELEGRAM_ADMIN_BOT_TOKEN=mock_tg_token TELEGRAM_DRIVER_BOT_TOKEN=mock_tg_token \
LARK_APP_ID=cli_mock_id LARK_APP_SECRET=mock_secret DEEPSEEK_API_KEY='' GEMINI_API_KEY='' \
UPGRADE_TEST_ALLOW_REMOTE=0 UPGRADE_TEST_ALLOW_REMOTE_DOCKER=0 \
PYTEST_ADDOPTS='tests/test_phone_links.py -k real_postgres_enforces_phone_link_trust_boundary' \
make test-postgres
```

Observed: **1 passed, 37 deselected in 2.64s**. The disposable database needed only the standalone phone table for this case. It proved source filtering, reverse lookup, latest trusted selection, atomic rejection of unverified overwrites/downgrades and actual question-source dispatch to the verified chat with a fake Telegram sender. No driver/CTV records or live provider calls were used. The runner cleans up its owned container.

### Actual browser surface

The existing Python browser regression could not run because Python Playwright is not installed. Instead, the already-installed Node Playwright/Chromium rendered the actual template in an isolated 390x844 browser fixture, blocking all non-fixture network requests. Observed:

- Vietnamese and English maintenance text is visible in the existing modal; no phone/query controls or history-fetch function; close works.
- No phone-history HTTP request is made.
- Existing submission-ID polling consumes a stubbed APPROVED result, updates the status bar, persists the result and stops normal polling.
- No page-script errors; mobile screenshot visually inspected with no clipped message/controls.

This is real-browser component/status verification, not a live deployment test or a claim that the Python browser suite ran. No dependency was added. CI/browser-job execution remains a separate rollout gate.

## Compatibility and rollout

Unverified historical mappings now intentionally fail closed and need self-contact/admin re-verification. Existing trusted mappings are protected from typed-number hijacking. Admin history clients must authenticate; anonymous web history is deliberately unavailable, not an empty-history response. Public submission-ID status remains available and PII-free.

Do not roll back to anonymous phone history, raw phone-as-chat fallback or treating legacy mappings as ownership proof. This change does not certify unrelated readiness/settings, full idempotency, durable notifications, moderation repairs or deployment/data-synchronization safety. No deployment was authorized or performed by this implementation.

## Ownership, recycling and retention policy

- A trusted binding never changes Telegram owner in place. A different account's verified contact/admin claim returns failure and pauses the disputed phone without changing its stored owner ID. Unverified/seed/typed claims cannot trigger that pause or remove it. A paused row cannot be reactivated simply by changing its source tag through the helper.
- Same-owner refresh preserves the established verification window. The existing `updated_at` column is the stable verification epoch for a trusted binding, not an activity timestamp; promotion from an unverified claim starts a new epoch. Do not backdate it to grant historical access.
- Adding a NEW phone to the SAME account intentionally retains its earlier trusted bindings; there is no inferred automatic revocation. Bot history selects the latest established trusted binding. Retired numbers must be explicitly revoked; a proved conflicting claim suspends the disputed number automatically rather than delivering new records to a stale owner.
- An operator must independently authorize and audit an ownership transfer, quiesce/drain related in-flight notification work, inspect the expected old owner, revoke that exact binding, and require fresh self-contact/admin verification for the new owner. `revoke_phone_telegram_link(phone, expected_telegram_user_id, pool)` is an INTERNAL maintenance primitive with a conditional delete, not an authenticated admin route; transport-level admin authentication/audit is the caller's responsibility. A wrong expected owner refuses revocation. No admin UI/endpoint or automatic transfer was added.
- After revocation/re-verification, the new binding has a new epoch. Reused phone numbers cannot automatically expose the previous owner's SQL or DEV history. This is conservative time-window authorization, NOT a claim that current phone possession proves an immutable owner for every historical submission. Exposing older records needs an explicit per-submission ownership contract.
- Phone verdict delivery also requires the ORIGINAL aware submission time. Initial ingress captures a server timestamp; moderation forwards the original stored ISO timestamp, never moderation time or a client answer. Missing/naive times fail closed. A mapping established after the record cannot receive its verdict, even after an administrator-approved phone transfer. The plain directory lookup is not, by itself, permission to send historical data.
- H1 now requires `phone_telegram_links` in `/ready`. The PostgreSQL CI step includes the marked tests in `tests/test_phone_links.py` with `UPGRADE_TEST_REQUIRE=1`; the ownership tests are no longer merely optional local coverage. GitHub Actions itself was not executed here and deployment is still unapproved.

## Observed ownership-correction verification

With the same explicit test-only environment/provider overrides as the initial command:

```bash
.venv/bin/python -B -m pytest -q tests/test_phone_links.py tests/test_party_notifications.py \
 tests/test_driver_bot.py::test_driver_bot_contact_sharing \
 tests/test_driver_bot.py::test_unverified_bot_history_never_reads_storage \
 tests/test_driver_bot.py::test_verified_bot_history_uses_only_bound_phone \
 tests/test_driver_bot.py::test_bot_history_is_not_disclosed_to_group_chat \
 tests/test_runtime_dependencies.py::test_health_is_liveness_only_and_ready_requires_db_schema \
 tests/test_user_facing_behavior.py::test_decision_recipient_follows_tg_response_source
```

Observed: **42 passed, 2 skipped in 3.13s**. The two real-PostgreSQL cases were then selected through the owned local runner, without rerunning the historical Wave 0 smoke checks:

```bash
ENVIRONMENT=test UPGRADE_TEST_ALLOW_REMOTE=0 UPGRADE_TEST_ALLOW_REMOTE_DOCKER=0 \
PYTEST_ADDOPTS='tests/test_phone_links.py -k real_postgres' make test-postgres
```

Observed: **2 passed, 37 deselected in 3.59s**. Dummy application settings/provider tokens were also supplied as in the earlier command. The real database checks cover different trusted IDs (including admin source), same-owner refresh/retained numbers, concurrent conflicting claims, guarded operator revocation, successful fresh verification after revocation, old-owner SQL/DEV history denial, post-verification history access, old verdict denial/new verdict delivery after transfer, and a physically renamed/missing mapping relation returning `/ready` 503. Focused non-database checks also cover missing/naive submission-time refusal and forwarding the original timestamp into delivery.

No production targets/access, live Telegram sends, full-baseline rerun, new schema/migration artifacts, Wave 3 implementation, commit, push or deployment were used. Web history and the public maintenance/status UI were not changed or re-certified by this ownership correction. The final results above supersede the intermediate 28-pass focused run and earlier two-case PostgreSQL timing.

## Webhook provenance, original time, DEV form identity and phone parity correction

### Webhook provenance

- Production Telegram webhook ingress (`/webhook/telegram*`) now fails closed with HTTP 503 when `TELEGRAM_WEBHOOK_SECRET` is empty or equal to the built-in `mock_webhook_secret`.
- A configured production secret is compared with Telegram's `X-Telegram-Bot-Api-Secret-Token` before `request.json()` or runner dispatch; absent/wrong headers return 401.
- Development/test behavior is retained for local fixtures; polling-only operation is unaffected because long-polling never calls the webhook route.
- `set_telegram_webhooks` refuses to register production webhooks with a missing/built-in secret.
- Focused HTTP tests use fake runners and prove missing/default config and absent/wrong headers never reach a contact write; a correct secret reaches the existing self-contact/third-party checks.

### Original submission time

- `received_at` is captured once at ingress and persisted to `survey_submissions.created_at` in both the extended insert and the minimal fallback insert. `ON CONFLICT` does not overwrite an existing original timestamp.
- DEV records continue to persist `submitted_at` from the same `received_at`.
- Dossiers, moderation delivery, SQL history and DEV history consume the stored original timestamp. Phone delivery requires an aware original timestamp at or after the binding's verification epoch; moderation time and client data are not substituted.
- Legacy rows with no independently known original ingress timestamp retain their existing database `created_at`; they are conservative: phone possession cannot authorize history or delivery before the binding epoch. No backfill is performed.

### DEV form identity

- DEV records now include the actual `active_form_id`.
- `_dev_record_dossier` preserves it; moderation resolves the form's recipient settings from that ID.
- Question-source DEV moderation uses the configured phone answer and verified mapping, with no driver/CTV fallback. Notifications-disabled forms remain disabled; CTV-configured forms retain the existing CTV path.

### Phone normalization parity

- Producer writes canonicalize accepted forms via `normalize_vn_phone`.
- `phone_normalization_sql()` applies the same accepted forms (`+84`, `84`, `0084`, bare nine-digit, canonical local) to SQL history joins, without suffix/last-nine matching.
- Admin SQL history and DEV history use the same canonical semantics. Legitimate noncanonical stored rows are matched; different numbers do not match.
- Binding owner, trusted source, private-chat and verification-time checks remain in force.

### Focused observed results

```bash
ENVIRONMENT=test ADMIN_USERNAME=h1-test-admin ADMIN_PASSWORD=h1-test-password \
APP_SECRET=h1_test_secret_only_0123456789abcdef \
DATABASE_URL=postgresql://test:test@127.0.0.1:1/h1_test \
REDIS_URL=redis://127.0.0.1:1/0 TELEGRAM_BOT_TOKEN=mock_tg_token \
TELEGRAM_ADMIN_BOT_TOKEN=mock_tg_token TELEGRAM_DRIVER_BOT_TOKEN=123:offline-test \
.venv/bin/python -B -m pytest -q tests/test_h1_corrections.py -m 'not postgres' \
 tests/test_history_privacy.py tests/test_phone_links.py tests/test_party_notifications.py \
 tests/test_driver_bot.py::test_driver_bot_contact_sharing \
 tests/test_driver_bot.py::test_unverified_bot_history_never_reads_storage \
 tests/test_driver_bot.py::test_verified_bot_history_uses_only_bound_phone \
 tests/test_driver_bot.py::test_bot_history_is_not_disclosed_to_group_chat \
 tests/test_form_engine.py::test_survey_history_admin_access \
 tests/test_privacy_and_tg_bot.py::test_survey_history_unauthorized_returns_401
```

Observed: **56 passed, 3 deselected in 3.58s**.

Real disposable PostgreSQL, including the new original-time/history/moderation boundary:

```bash
ENVIRONMENT=test ... TELEGRAM_DRIVER_BOT_TOKEN=123:offline-test \
 timeout 300 make test-postgres
```

Observed: **17 passed, 33 deselected in 3.64s** for the marked PostgreSQL set, followed by **10 passed, 1 deselected in 2.74s** for the H1 non-PostgreSQL regressions. The deterministic writer test controls ingress and later persistence times and proves `created_at` equals the former, not the latter. The DEV writer test feeds the actual captured record through dossier construction and moderation for `question`, `none`, and `ctv` policies. The H1 real-PostgreSQL test covers a submission received before verification (excluded), one after verification (eligible for history and moderation delivery), equivalent country-code/bare/canonical phone forms, a different phone, and the binding epoch. The test uses the owned disposable runner and no production target.

The CI `postgres-harness` job now runs `tests/test_postgres_harness.py`, `tests/test_phone_links.py`, and `tests/test_h1_corrections.py` with `UPGRADE_TEST_REQUIRE=1`, then `-m 'not postgres' tests/test_h1_corrections.py`, then `tests/test_postgres_runner.py`; GitHub Actions was not executed here. The local harness script runs the same marked set and the H1 non-PostgreSQL regressions.

No production access, deployment, commit, push, live provider calls, or full-suite baseline rerun was performed.
