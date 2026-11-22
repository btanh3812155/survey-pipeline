#!/usr/bin/env bash
# Apply a Wave migration transactionally to an isolated disposable PostgreSQL target.
# This is a TEST-ONLY helper. Production application is a separate, unauthorized step.
set -euo pipefail
FILE="${1:?usage: $0 scripts/migrations/001_submission_attempts.sql}"
: "${UPGRADE_TEST_DATABASE_URL:?UPGRADE_TEST_DATABASE_URL is required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${UPGRADE_TEST_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi
PINNED_DSN="$(cd "$ROOT" && PYTHONPATH="$ROOT" UPGRADE_TEST_ALLOW_REMOTE=0 "$PYTHON_BIN" - "$UPGRADE_TEST_DATABASE_URL" <<'PY'
import sys
from tests.postgres_harness import resolve_test_database_url

try:
    print(resolve_test_database_url(sys.argv[1]))
except Exception as exc:
    raise SystemExit(str(exc))
PY
)" || exit 1


PGOPTIONS="${UPGRADE_TEST_PGOPTIONS:-} -c lock_timeout=5s -c statement_timeout=60s"
SHA="$(sha256sum "$FILE" | cut -d ' ' -f 1)"
SHA_001="$(sha256sum "$ROOT/scripts/migrations/001_submission_attempts.sql" | cut -d ' ' -f 1)"
SHA_002="$(sha256sum "$ROOT/scripts/migrations/002_outbox_delivery.sql" | cut -d ' ' -f 1)"
SHA_006="$(sha256sum "$ROOT/scripts/migrations/006_admin_accounts.sql" | cut -d ' ' -f 1)"
SHA_007="$(sha256sum "$ROOT/scripts/migrations/007_admin_account_bin.sql" | cut -d ' ' -f 1)"
SHA_008="$(sha256sum "$ROOT/scripts/migrations/008_admin_module_registry.sql" | cut -d ' ' -f 1)"
if [ -n "${UPGRADE_TEST_POSTGRES_CONTAINER:-}" ]; then
  CONTAINER="$UPGRADE_TEST_POSTGRES_CONTAINER"
  ENDPOINT="${UPGRADE_TEST_DOCKER_ENDPOINT:?UPGRADE_TEST_DOCKER_ENDPOINT is required with the container runner}"
  case "$ENDPOINT" in
    unix://*|npipe://*|fd://*) ;;
    *) echo "refusing non-local Docker endpoint '$ENDPOINT'" >&2; exit 1 ;;
  esac
  unset DOCKER_CONTEXT
  export DOCKER_HOST="$ENDPOINT"
  CONTAINER_ID="$(docker inspect --format '{{.Id}}' "$CONTAINER" 2>/dev/null || true)"
  if [ -z "$CONTAINER_ID" ]; then
    echo "refusing unknown PostgreSQL container '$CONTAINER'" >&2
    exit 1
  fi
  OWNED="$(docker inspect --format '{{ index .Config.Labels "survey-pipeline-wave0" }}' "$CONTAINER_ID" 2>/dev/null || true)"
  if [ "$OWNED" != "true" ]; then
    echo "refusing unowned PostgreSQL container '$CONTAINER'" >&2
    exit 1
  fi
  DB="${UPGRADE_TEST_POSTGRES_DB:?UPGRADE_TEST_POSTGRES_DB is required with the container runner}"
  case "$DB" in
    *[!A-Za-z0-9_]*|"") echo "refusing non-literal container database selector '$DB'" >&2; exit 1 ;;
    *_test) ;;
    *) echo "refusing non-test container database '$DB'" >&2; exit 1 ;;
  esac
  case "$(printf '%s' "$DB" | tr '[:upper:]' '[:lower:]')" in
    *production*|*prod*|*surveydb*) echo "refusing production-looking container database '$DB'" >&2; exit 1 ;;
  esac
  # Pin execution to the immutable container id and its local TCP endpoint.
  PSQL=(docker exec -i -e PGOPTIONS="$PGOPTIONS" "$CONTAINER_ID" psql -h 127.0.0.1 -p 5432 -U postgres -d "$DB")
else
  command -v psql >/dev/null 2>&1 || { echo "psql is required" >&2; exit 1; }
  PSQL=(psql "$PINNED_DSN")
fi

ACTUAL_DB="$("${PSQL[@]}" -X -Atqc 'SELECT current_database();')"
case "$ACTUAL_DB" in
  *_test) ;;
  *) echo "refusing effective database '$ACTUAL_DB' (must end in _test)" >&2; exit 1 ;;
esac
case "$(printf '%s' "$ACTUAL_DB" | tr '[:upper:]' '[:lower:]')" in
  *production*|*prod*|*surveydb*) echo "refusing production-looking effective database '$ACTUAL_DB'" >&2; exit 1 ;;
esac

if [ -n "${UPGRADE_TEST_POSTGRES_CONTAINER:-}" ]; then
  "${PSQL[@]}" -X -v ON_ERROR_STOP=1 -v migration_sha256="$SHA" -v migration_001_sha256="$SHA_001" -v migration_002_sha256="$SHA_002" -v migration_006_sha256="$SHA_006" -v migration_007_sha256="$SHA_007" -v migration_008_sha256="$SHA_008" < "$FILE"
else
  PGOPTIONS="$PGOPTIONS" "${PSQL[@]}" -X -v ON_ERROR_STOP=1 -v migration_sha256="$SHA" -v migration_001_sha256="$SHA_001" -v migration_002_sha256="$SHA_002" -v migration_006_sha256="$SHA_006" -v migration_007_sha256="$SHA_007" -v migration_008_sha256="$SHA_008" < "$FILE"
fi
