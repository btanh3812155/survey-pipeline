# Wave 1 runtime contract

## Environment and production settings

- `ENVIRONMENT` is required and must be exactly `production`, `development`, or `test`.
- Missing/unknown values fail settings construction; there is no implicit development fallback.
- Production rejects:
  - empty or built-in `ADMIN_PASSWORD`;
  - empty, built-in, or shorter-than-32-character `APP_SECRET`;
  - missing/short `APP_SECRET`;
  - non-PostgreSQL or built-in-default `DATABASE_URL`.
- Validation errors hide all input values; an invalid secret cannot expose credentials from another field.
- Development/test keep the existing labelled DEV/test storage behavior.

## Startup policy

- Production starts only after PostgreSQL `SELECT 1` succeeds.
- Production PostgreSQL startup failure raises from lifespan. HTTP, Redis, and any
  partially opened PostgreSQL pool are closed through the shared `finally` path;
  cancellation during pool open/health checking also closes the unpublished pool.
- Redis remains an optional accelerator; failure falls back to the existing in-memory cache.
- Development/test retain the labelled dev-storage fallback when PostgreSQL is unavailable.

## Health and readiness

- `GET /health` remains the liveness endpoint and is independent of PostgreSQL.
- `GET /ready` is 200 only when the configured PostgreSQL writer is reachable and the current writer tables exist:
  `survey_submissions`, `outbox_events`, `authorized_users`, `driver_identity_vault`,
  `driver_registered_vehicles`. H1 additionally requires `phone_telegram_links`; a missing relation yields `503 schema_incomplete` (see `WAVE2_H1_PHONE_BOUNDARY.md`).
- `/ready` returns 503 with `postgresql_unavailable` or `schema_incomplete` when the writer is not usable.
- Future-wave tables are not required by this gate.
- Redis is intentionally not a readiness dependency because the application retains
  its in-memory fallback; PostgreSQL is the mandatory writer dependency.

## Test-mode authority

The server, not the client, authorizes test/bypass flags.

- Development/test may use `is_test_mode` and retain labelled `DEV-*` submissions in separate storage.
- Production allows test flags only when the server-side form schema has `settings.is_test_mode=true`.
- Test/bypass authorization runs before any idempotent cached response. A cached
  `TEST_DEV_MODE`/`PROD_MODE` payload with the wrong current mode is rejected (409).
- A client flag on a normal production form is rejected with 403.
- `bypass_img_check` without an authorized test mode is rejected with 400.
- A production request never becomes a persisted-success response when the writer is unavailable or fails.

## Submission failure behavior

- Production submissions return 503 before processing when PostgreSQL is unavailable at entry.
- A production persistence failure returns 503; it is not acknowledged as a successful persisted verdict.
- Development keeps the existing interruption/crash-guard path for diagnostics.

## Verification

Probe wiring: `scripts/smoke_prod.sh`, `scripts/deploy_prod.sh`, and the CI
deploy verification now check `/ready` in addition to `/health`.

Focused suite:

```bash
.venv/bin/python -m pytest -q tests/test_runtime_dependencies.py
```

The suite covers settings validation, secret non-echo, startup failure cleanup, `/health` versus `/ready`, schema/connection loss readiness, production submission fail-closed behavior, and server-side test/bypass authority. Real PostgreSQL schema behavior remains covered by the Wave 0 disposable harness.
