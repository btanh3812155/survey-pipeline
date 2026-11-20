# Wave 3 idempotent writes and private media boundary

**Status:** locally implemented on top of H1; not deployed. Migration 001 and writer activation remain separate production gates. No production access, migration, commit, push or rollout is claimed.

## Scope

- Browser attempt keys use native `crypto.getRandomValues` only and survive timeout/lost-response/reload. A FormData retry snapshot remains in memory; only key/unresolved metadata is persisted. A reload without the snapshot requires an explicit confirmation before a separate attempt; reset and bypass confirmation happen before any destructive UI cleanup; changed payload and same-size/type image replacement require explicit separate-attempt confirmation; a successful response clears metadata and rotates the key.
- PostgreSQL `submission_idempotency` is the atomic normal/recovery owner. Scope is environment + form + mode + HMAC-canonicalized phone. The fingerprint covers canonical form/mode, normalized answers, ordered `(question_id, slot_index, SHA-256(bytes))`, signature hash, GPS and CTV; phone-transport aliases and upload placeholders are excluded because scope carries the canonical phone.
- The first recovery snapshot includes the attempt identity. A committed attempt is resolved before AI/image evaluation and media writes. A fresh connection rechecks the key after an uncertain commit; a still-unknown outcome retains media and returns a bounded retryable failure.
- Same key + same fingerprint replays the committed JSON response. Same key + different fingerprint is `409`. An unresolved reservation is `503` and is rolled back rather than committed as a new touch.
- Crash recovery reserves the same attempt before inserting an interruption row and outbox event; it commits the interruption response to the attempt. Cancellation/failure recovery never allocates a second owner.
- The extended insert runs inside `SAVEPOINT survey_extended_insert`; only an `UndefinedColumn` compatibility failure rolls back to it. Constraint, serialization and connection errors still abort the transaction.
- Migration 001 validates existing migration-owned columns, primary-key shape, state constraint, expiry index, media columns and ledger definition before recording the checksum. Migration target validation uses the Wave 0 effective-conninfo boundary and pins the actual test container/database; rejection occurs before any psql or docker exec.
- The state CHECK must match the server-deparsed migration-owned `state IN ('RESERVED', 'COMMITTED')` expression and be validated. The comparison uses an empty transaction-local reference table with the same column type; extra states and alternate constraint shapes fail closed without changing existing constraints. Valid-state insert/default checks remain. The reference table is dropped on commit or rollback.
- Private submission bytes live under `data/submission_photos/<id>/`, are addressed by a DB manifest (`media_manifest`, `media_version`) and are not served by the public `/media` mount. Admins fetch them only through authenticated routes. Historical empty manifests retain the flat-file fallback.
- Admin viewer, Telegram dossier, Drive mirror/backfill and outbox consumers use one private-manifest adapter. Empty historical manifests use only the trusted private flat-file root. A non-empty manifest is authoritative. Drive refresh stages and validates every expected revision before atomically publishing the mirror, so missing/hash-mismatched/write-failed refreshes preserve existing evidence and report failure.
- Telegram repair buttons emit a concrete identity; the writer selects exactly one `(question_id, slot_index, flat_index)` entry and preserves that identity and all untouched entries. Replacement verifies the requesting account's trusted phone binding, including the binding epoch relative to the original submission, against the specific submission. An empty historical manifest is seeded from all legacy slots so untouched evidence survives. The immutable revision, manifest/version, approval status and legacy `ALL` work commit together; wrong requester, unknown/ambiguous slot, post-reassignment old rows and already-finalized rows do not report success. A missing authoritative pool fails closed without filesystem writes or approval acknowledgement.

## Activation

1. Take and verify a database backup, and record the current `/ready` response.
2. Apply the additive migration against the approved target with the checked-in command:
   `bash scripts/apply_migration.sh scripts/migrations/001_submission_attempts.sql`
   The script validates the effective test target, computes the file checksum, uses psql variable interpolation outside the dollar-quoted body, and takes the transaction-scoped advisory lock. Production application is separate and unauthorized.
3. Deploy the writer only after the migration ledger and columns exist. `/ready` compares the ledger checksum to the checked-in migration and fails closed otherwise.
4. Keep the old application available for rollback. Rollback stops new writes but retains `submission_idempotency` and media columns; committed attempts and private media are never dropped.

## Local evidence

Commands and observed results:

- `make test-postgres`: **35 passed, 47 deselected**; followed by **10 passed, 1 deselected** for the H1 non-PostgreSQL phase. Includes the actual checked-in migration command, exact state-domain rejection, schema drift cases, route replay/conflict/concurrency/ack-loss, recovery, savepoint, private-media repair, and a separate-process SIGKILL during staged submission writes followed by same-key retry.
- Focused non-PostgreSQL: **72 passed, 1 skipped, 17 deselected**. The Python Playwright status-bar test is skipped because that interpreter lacks Playwright; Node Playwright browser acceptance ran separately. H1 preservation suite result of **71 passed, 3 deselected** was reported in the earlier pass and not rerun this turn.
- Browser acceptance: `node tests/test_wave3_browser.cjs` — **9 passed** against the real inline form in Chromium with requests blocked/stubbed. Covers multipart retry/first committed ID, reload, changed text and same-size/type Blob confirmation, reset/bypass cancel, encoder single-flight, and mode/reset invalidation during encoding.
- The known pre-existing `tests/test_submissions_viewer.py::test_submissions_viewer_authorized` text assertion was not used as Wave 3 evidence and was not rerun in the final focused set.
- Exact-state correction self-check: **4 passed, 21 deselected** on disposable PostgreSQL 16.16 using the four migration-focused tests in `tests/test_wave3_attempts.py`. Covers the actual `scripts/apply_migration.sh` command, valid first apply/rerun, extra-state rejection with no migration ledger entry, single-defect incompatible schemas, checksum rejection, rollback, fresh schema and stable legacy fixture. This is a narrow local check, not a rerun of the reported full suites. Migration 001's file checksum changes with this correction; disposable databases using the prior checksum must be recreated, not have their ledgers rewritten.
- Process-kill regression: `tests/test_wave3_process_crash.py` is selected by the PostgreSQL runner and CI job. The submit process is SIGKILLed after submission, attempt and outbox writes are staged but before commit; no rows become visible, uploaded media remains, and same-key retry commits exactly one submission, attempt and `ALL` event. The final spawn-based worker also passed alone (**1 passed**), avoiding inherited threads/locks from the parent test process.

## Residual gaps

- No production/staging migration, writer activation, deployment, production browser check or real GitHub Actions run was performed.
- Live Telegram/Drive/R2 provider delivery remains unproven. All local provider calls were mocked or blocked; local manifest/mirror/replacement behavior is what these checks verify.
- Python Playwright status-bar tests remain skipped because that interpreter lacks Playwright; the Node Chromium acceptance script is runnable separately. The full unrelated baseline was not rerun.
