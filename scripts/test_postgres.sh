#!/usr/bin/env bash
# Wave 0 disposable PostgreSQL/pgvector harness. No production host or credential.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${UPGRADE_TEST_POSTGRES_IMAGE:-pgvector/pgvector:pg16}"
PORT="${UPGRADE_TEST_POSTGRES_PORT:-55432}"
DB="${UPGRADE_TEST_POSTGRES_DB:-survey_pipeline_test}"
PASSWORD="${UPGRADE_TEST_POSTGRES_PASSWORD:-test_only}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
DRY_RUN="${UPGRADE_TEST_POSTGRES_DRY_RUN:-0}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for the disposable PostgreSQL harness" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import psycopg' >/dev/null 2>&1; then
  echo "$PYTHON cannot import psycopg" >&2
  exit 1
fi

# Establish the Docker endpoint before any mutation. Remote daemons require an
# explicit opt-in so a misconfigured context cannot touch another host.
CONTEXT="$(docker context show 2>/dev/null || true)"
if [ -z "$CONTEXT" ]; then
  echo "cannot determine the active Docker context" >&2
  exit 1
fi
if [ -n "${UPGRADE_TEST_DOCKER_CONTEXT:-}" ] && [ "$CONTEXT" != "$UPGRADE_TEST_DOCKER_CONTEXT" ]; then
  echo "refusing Docker context '$CONTEXT'; expected '$UPGRADE_TEST_DOCKER_CONTEXT'" >&2
  exit 1
fi
ENDPOINT="$(docker context inspect "$CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
case "$ENDPOINT" in
  unix://*|npipe://*|fd://*) ;;
  *)
    if [ "${UPGRADE_TEST_ALLOW_REMOTE_DOCKER:-0}" != "1" ]; then
      echo "refusing remote Docker endpoint '$ENDPOINT' (set UPGRADE_TEST_ALLOW_REMOTE_DOCKER=1 only for a disposable daemon)" >&2
      exit 1
    fi
    ;;
esac

# Never delete a pre-existing container. Caller-provided names must be unused;
# otherwise generate a unique identity owned by this invocation.
NAME="${UPGRADE_TEST_POSTGRES_NAME:-}"
if [ -n "$NAME" ]; then
  if ! [[ "$NAME" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "invalid UPGRADE_TEST_POSTGRES_NAME '$NAME'" >&2
    exit 1
  fi
  if [ -n "$(docker ps -a --filter "name=^/${NAME}$" --format '{{.ID}}')" ]; then
    echo "refusing to reuse existing container '$NAME'" >&2
    exit 1
  fi
else
  NAME="survey-pipeline-wave0-pg-$$-$(date +%s)"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN context=$CONTEXT endpoint=$ENDPOINT name=$NAME db=$DB port=$PORT"
  exit 0
fi

# Own the container identity; cleanup must never target a name or another container.
CID="$(docker run -d --name "$NAME" \
  --label "survey-pipeline-wave0=true" \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD="$PASSWORD" \
  -e POSTGRES_DB="$DB" \
  -p "127.0.0.1:${PORT}:5432" \
  "$IMAGE")"
if [ -z "$CID" ]; then
  echo "docker run did not return a container id" >&2
  exit 1
fi
cleanup() {
  docker rm -f "$CID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  # pg_isready can be true while the entrypoint is still creating POSTGRES_DB.
  if docker exec "$CID" psql -U postgres -d "$DB" -Atqc 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! docker exec "$CID" psql -U postgres -d "$DB" -Atqc 'SELECT 1' >/dev/null 2>&1; then
  echo "disposable PostgreSQL did not become ready" >&2
  docker logs "$CID" >&2 || true
  exit 1
fi

export UPGRADE_TEST_DATABASE_URL="postgresql://postgres:${PASSWORD}@127.0.0.1:${PORT}/${DB}"
export UPGRADE_TEST_REQUIRE=1
export UPGRADE_TEST_POSTGRES_CONTAINER="$CID"
export UPGRADE_TEST_POSTGRES_DB="$DB"
export UPGRADE_TEST_DOCKER_ENDPOINT="$ENDPOINT"
export UPGRADE_TEST_DOCKER_CONTEXT="$CONTEXT"
cd "$ROOT"
"$PYTHON" -m pytest -q -m postgres tests/test_postgres_harness.py tests/test_phone_links.py tests/test_h1_corrections.py tests/test_wave3_attempts.py tests/test_wave3_route.py tests/test_wave3_process_crash.py
"$PYTHON" -m pytest -q tests/test_h1_corrections.py -m 'not postgres'
