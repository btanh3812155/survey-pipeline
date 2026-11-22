#!/usr/bin/env bash
# This is also sourced by the release runner; it never claims or creates work.
set -euo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

release_args() {
  [[ "$TARGET" == production || "$TARGET" == staging ]] || die 'choose --target production|staging'
  [[ "$SCHEMA" == 002 ]] || die 'choose --schema 002 explicitly'
  RELEASE_ENV=production
  [[ "$TARGET" != staging ]] || RELEASE_ENV=development
  [[ ${RELEASE_HOST:-} =~ ^([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || die 'set RELEASE_HOST'
  [[ ${RELEASE_ROOT:-} =~ ^(/[a-zA-Z0-9_-][a-zA-Z0-9_.-]*)+$ ]] || die 'set an absolute RELEASE_ROOT'
  local name
  for name in RELEASE_APP RELEASE_WORKER RELEASE_DB_CONTAINER; do
    [[ ${!name:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || die "set $name"
  done
  [[ "$RELEASE_APP" != "$RELEASE_WORKER" && "$RELEASE_APP" != "$RELEASE_DB_CONTAINER" && "$RELEASE_WORKER" != "$RELEASE_DB_CONTAINER" ]] || die 'container selectors must be distinct'
  [[ ${RELEASE_DB_NAME:-} =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die 'set RELEASE_DB_NAME'
  [[ ${RELEASE_BUILD_ID:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ && "$RELEASE_BUILD_ID" != local ]] || die 'set an explicit RELEASE_BUILD_ID (not local)'
  RELEASE_DB_USER=${RELEASE_DB_USER:-postgres}
  [[ "$RELEASE_DB_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || die 'invalid RELEASE_DB_USER'
  RELEASE_TIMEOUT=${RELEASE_TIMEOUT:-90}
  [[ "$RELEASE_TIMEOUT" =~ ^[1-9][0-9]{0,3}$ ]] || die 'invalid RELEASE_TIMEOUT'
}

run_bundle() {
  local bundle=$1 command arg; shift
  command='set -eu; umask 077; d=$(mktemp -d); trap '\''rm -rf -- "$d"'\'' EXIT; tar --no-same-owner --same-permissions -xf - -C "$d"; bash "$d/runner.sh" "$d"'
  for arg in "$TARGET" "$SCHEMA" "$RELEASE_ENV" "$RELEASE_ROOT" "$RELEASE_APP" "$RELEASE_WORKER" "$RELEASE_DB_CONTAINER" "$RELEASE_DB_NAME" "$RELEASE_DB_USER" "$RELEASE_TIMEOUT" "$RELEASE_BUILD_ID" "$@"; do
    command+=" $(printf '%q' "$arg")"
  done
  tar -C "$bundle" -cf - . | ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=yes -- "$RELEASE_HOST" "$command"
}

bundle_base() {
  mkdir -p "$1/migrations"
  cp -- "$REPO_ROOT/scripts/smoke_prod.sh" "$1/smoke.sh"
  local name path
  for name in 001_submission_attempts 002_outbox_delivery 003_outbox_operations 004_image_dinov2_exceptions 005_image_dhash_exceptions 006_admin_accounts 007_admin_account_bin 008_admin_module_registry 009_drive_exports 010_drive_export_form_settings 011_engine_modules 012_db_trash; do
    path="$REPO_ROOT/scripts/migrations/$name.sql"
    [[ -f "$path" && $(realpath -e -- "$path") == "$path" ]] || die "missing or unsafe migration: $name"
    cp -- "$path" "$1/migrations/"
  done
}

remote_args() {
  BUNDLE=$1 TARGET=$2 SCHEMA=$3 RELEASE_ENV=$4 RELEASE_ROOT=$5 RELEASE_APP=$6 RELEASE_WORKER=$7
  RELEASE_DB_CONTAINER=$8 RELEASE_DB_NAME=$9 RELEASE_DB_USER=${10} RELEASE_TIMEOUT=${11}
  RELEASE_BUILD_ID=${12}
  [[ -d "$RELEASE_ROOT" && $(realpath -e -- "$RELEASE_ROOT") == "$RELEASE_ROOT" ]] || die 'target root is missing or contains a symlink'
  [[ -d "$RELEASE_ROOT/data" && $(realpath -e -- "$RELEASE_ROOT/data") == "$RELEASE_ROOT/data" ]] || die 'target private-data directory is missing or contains a symlink'
  # Use host-local Docker only, even if the remote account has a client context.
  unset DOCKER_CONTEXT DOCKER_HOST
  docker() { command docker --host unix:///var/run/docker.sock "$@"; }
}

verify_target() {
  local codes=${1:-/dev/null} identity
  # Inspect the immutable container IDs, actual configured environment, shared data
  # mount and database endpoint before acting. Never display Config.Env/DSNs.
  identity=$(docker inspect --type container "$RELEASE_APP" "$RELEASE_WORKER" "$RELEASE_DB_CONTAINER" |
    python3 -c '
import json, re, sys
from urllib.parse import unquote, urlsplit

environment, root, database, paths, build_id = sys.argv[1:]
app, worker, db = json.load(sys.stdin)

def require(ok, message):
    if not ok:
        raise SystemExit("target check: " + message)

def env(container):
    return dict(pair.split("=", 1) for pair in container["Config"]["Env"] if "=" in pair)

def mounted(container, path, writable=False):
    mounts = [m for m in container["Mounts"] if path == m["Destination"] or path.startswith(m["Destination"].rstrip("/") + "/")]
    if not mounts:
        return False
    m = max(mounts, key=lambda mount: len(mount["Destination"]))
    return m["Type"] == "bind" and m["Source"] + path[len(m["Destination"]):] == root + path[4:] and (not writable or m["RW"])

def endpoint(container):
    settings = env(container)
    require(settings.get("ENVIRONMENT") == environment, "app/worker environment differs from the requested target")
    require(settings.get("APP_BUILD_ID") == build_id, "app/worker APP_BUILD_ID differs from the approved release build")
    require(container["Config"]["WorkingDir"] == "/app", "app/worker must use /app as the working directory")
    require(not container["HostConfig"].get("ExtraHosts"), "unexpected host mapping override")
    require(not any(settings.get(k) for k in ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGOPTIONS")), "unexpected PostgreSQL environment override")
    try:
        uri = urlsplit(settings.get("DATABASE_URL", ""))
        valid = (uri.scheme in ("postgres", "postgresql") and uri.hostname and
                 not uri.query and not uri.fragment and unquote(uri.path) == "/" + database)
        port = uri.port or 5432
    except ValueError:
        valid = False
    require(valid, "DATABASE_URL must select the requested database without overrides")
    matched = False
    networks = container["NetworkSettings"]["Networks"]
    for name, network in db["NetworkSettings"]["Networks"].items():
        if not network["IPAddress"] or name not in networks or network["NetworkID"] != networks[name]["NetworkID"]:
            continue
        aliases = (network.get("Aliases") or []) + (network.get("DNSNames") or [])
        matched |= uri.hostname in (network["IPAddress"], db["Name"].lstrip("/"), *aliases)
    require(matched, "DATABASE_URL does not select the inspected PostgreSQL container on a shared network")
    require(mounted(container, "/app/data", writable=True), "missing shared writable private-data bind mount")
    require(container["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", "app/worker need an unless-stopped policy so rollback stays paused across daemon restarts")
    require(not any(m["Destination"].startswith("/app/data/") for m in container["Mounts"]), "unexpected nested private-data mount")
    return uri.hostname, port

require(db["State"]["Running"], "PostgreSQL container is not running")
app_endpoint = endpoint(app)
require(app_endpoint == endpoint(worker), "app and worker database endpoints disagree")
cfg = worker["Config"]
require(cfg["Cmd"] == ["python", "-m", "workers.outbox_worker"] and not cfg["Entrypoint"], "worker must run the supervised outbox module directly")
app_env, worker_env = env(app), env(worker)
for key in ("R2_ACCOUNT_ID", "R2_PRIVATE_BUCKET_NAME"):
    require(app_env.get(key, "") == worker_env.get(key, ""), "app and worker private object destinations disagree")
if app_env.get("R2_PRIVATE_BUCKET_NAME"):
    require(all(s.get(k) for s in (app_env, worker_env) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")),
            "private object storage credentials are not provisioned for both services")
worker_id = worker_env.get("OUTBOX_WORKER_ID", "")
require(re.fullmatch(r"[a-zA-Z0-9_.:-]+", worker_id), "worker needs an explicit OUTBOX_WORKER_ID")
require(worker_env.get("OUTBOX_V2_WORKER_ENABLED", "").lower() == "true", "worker polling is disabled")
require(worker_env.get("DRIVE_SYNC_INTERVAL_SEC") == "0", "disable the worker periodic Drive loop for this release path")
with open(paths) as selected:
    for rel in selected.read().splitlines():
        if not rel.startswith("workers/"):
            require(mounted(app, "/app/" + rel), "app does not bind the release path " + rel)
        if not (rel.startswith(("routers/", "static/")) or rel == "main.py"):
            require(mounted(worker, "/app/" + rel), "worker does not bind the release path " + rel)
print(app["Id"], worker["Id"], db["Id"], worker_id, app_endpoint[1])
' "$RELEASE_ENV" "$RELEASE_ROOT" "$RELEASE_DB_NAME" "$codes" "$RELEASE_BUILD_ID") || die 'could not verify target topology'
  read -r APP_ID WORKER_ID DB_ID WORKER_NAME DB_PORT <<<"$identity"
  [[ -n "$APP_ID" && -n "$WORKER_ID" && -n "$DB_ID" && -n "$WORKER_NAME" && -n "$DB_PORT" ]] || die 'incomplete target identity'
}

target_pg() {
  local mode=$1 program=$2; shift 2
  local opts='-c search_path=public -c lock_timeout=5s -c statement_timeout=60s'
  [[ "$mode" == write ]] || opts+=' -c default_transaction_read_only=on'
  docker exec -i "$DB_ID" env -u PGHOSTADDR -u PGSERVICE -u PGDATABASE "PGOPTIONS=$opts" \
    "$program" -w -h /var/run/postgresql -p "$DB_PORT" -U "$RELEASE_DB_USER" -d "$RELEASE_DB_NAME" "$@"
}

target_sql() {
  local mode=$1; shift
  target_pg "$mode" psql -X -qAt -v ON_ERROR_STOP=1 -v "release_database=$RELEASE_DB_NAME" "$@"
}

guard_sql() {
  cat <<'SQL'
SELECT current_database() = :'release_database' AND current_schema() = 'public'
       AND NOT pg_is_in_recovery() AS release_target_ok \gset
\if :release_target_ok
\else
  \echo PostgreSQL target database/schema does not match the release
  SELECT 1/0;
\endif
SQL
}

load_migrations() {
  local file hash
  MIGRATIONS=("$BUNDLE"/migrations/[0-9][0-9][0-9]_*.sql)
  EXPECTED_LEDGER=()
  for file in "${MIGRATIONS[@]}"; do
    hash=$(sha256sum -- "$file"); hash=${hash%% *}
    file=${file##*/}
    EXPECTED_LEDGER+=("${file%%_*}|$hash")
  done
}

check_ledger() {
  local result i
  result=$({ guard_sql; cat <<'SQL'
SELECT to_regclass('public.schema_migrations') IS NOT NULL AS release_has_ledger \gset
\if :release_has_ledger
SELECT version, checksum FROM public.schema_migrations ORDER BY version;
\endif
SQL
  } | target_sql read) || die 'PostgreSQL target or ledger check failed'
  LEDGER=()
  [[ -z "$result" ]] || mapfile -t LEDGER <<<"$result"
  for i in "${!LEDGER[@]}"; do
    [[ ${LEDGER[i]} == "${EXPECTED_LEDGER[i]:-}" ]] || die 'unexpected migration version or checksum; no changes made to the ledger'
  done
}

require_schema() {
  check_ledger
  [[ ${#LEDGER[@]} == "${#EXPECTED_LEDGER[@]}" ]] || die 'required migration(s) missing'
}

worker_probe() {
  local started
  started=$(docker inspect -f '{{.State.StartedAt}}' "$WORKER_ID") || return
  docker exec -w /app -e PGCONNECT_TIMEOUT=5 -e 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=5000' \
    "$WORKER_ID" python -B -m workers.outbox_worker --health |
    python3 -c '
from datetime import datetime, timezone
import json, math, sys

try:
    data = json.load(sys.stdin)
    age = data["heartbeat_age_seconds"]
    started = datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00"))
    uptime = (datetime.now(timezone.utc) - started).total_seconds()
    expected = {"BILLING", "R2_UPLOAD", "GDRIVE_EXPORT", "LARK_SYNC", "TELEGRAM_NOTIFY"}
    if (data["ready"] is not True or data["schema_version"] != sys.argv[1] or data["health_schema_version"] != "003"
            or data["worker_id"] != sys.argv[2] or data["build_id"] != sys.argv[4]
            or not expected <= set(data["supported_targets"]) or type(age) not in (int, float)
            or not math.isfinite(age) or not 0 <= age <= min(120, uptime)):
        raise ValueError
except (KeyError, TypeError, ValueError):
    raise SystemExit("worker capability or post-start heartbeat check failed")
print(json.dumps({"worker_id": data["worker_id"], "build_id": data["build_id"], "heartbeat_age_seconds": age, "supported_targets": sorted(expected)}))
' "$SCHEMA" "$WORKER_NAME" "$started" "$RELEASE_BUILD_ID"
}

app_probe() {
  docker exec "$APP_ID" python -B -c '
import http.client, json, sys

for path, status in (("/health", "healthy"), ("/ready", "ready")):
    try:
        client = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
        client.request("GET", path)
        response = client.getresponse()
        data = json.loads(response.read(65536))
        if response.status != 200 or data.get("status") != status or data.get("environment") != sys.argv[1]:
            raise ValueError
    except (OSError, AttributeError, ValueError, http.client.HTTPException):
        raise SystemExit(path + " did not confirm the requested environment and status")
    finally:
        client.close()
    print(path + " OK")
' "$RELEASE_ENV"
}

queue_probe() {
  local metrics
  metrics=$({ guard_sql; cat <<'SQL'
SELECT json_build_object(
    'pending', count(*) FILTER (WHERE status = 'PENDING'),
    'processing', count(*) FILTER (WHERE status = 'PROCESSING'),
    'failed', count(*) FILTER (WHERE status = 'FAILED'),
    'uncertain', count(*) FILTER (WHERE terminal_reason = 'DELIVERY_UNCERTAIN'),
    'oldest_open_age_seconds', EXTRACT(epoch FROM now() - min(created_at) FILTER (WHERE status IN ('PENDING','PROCESSING'))),
    'oldest_failed_age_seconds', EXTRACT(epoch FROM now() - min(created_at) FILTER (WHERE status = 'FAILED')),
    'oldest_uncertain_age_seconds', EXTRACT(epoch FROM now() - min(created_at) FILTER (WHERE terminal_reason = 'DELIVERY_UNCERTAIN'))
) FROM public.outbox_events;
SQL
  } | target_sql read) || return
  printf 'OUTBOX_METRICS=%s\n' "$metrics"
}

wait_probe() {
  local deadline=$((SECONDS + RELEASE_TIMEOUT))
  until "$@"; do
    (( SECONDS < deadline )) || die "timed out: $1"
    sleep 2
  done
}

remote_smoke() {
  require_schema
  worker_probe
  app_probe
  queue_probe
  printf 'SMOKE_RESULT={"ok":true,"target":"%s","schema":"%s"}\n' "$TARGET" "$SCHEMA"
}

local_smoke() {
  TARGET= SCHEMA=
  while (( $# )); do
    case "$1" in
      --target) TARGET=${2:?missing target}; shift ;;
      --schema) SCHEMA=${2:?missing schema}; shift ;;
      -h|--help) printf 'usage: %s --target production|staging --schema 002\nUses the same RELEASE_* target configuration as deploy_prod.sh.\n' "$0"; return ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done
  release_args
  REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
  # EXIT runs after this function returns, so keep the cleanup path in scope.
  local bundle cleanup
  bundle=$(mktemp -d)
  printf -v cleanup 'rm -rf -- %q' "$bundle"
  trap "$cleanup" EXIT
  bundle_base "$bundle"
  cat >"$bundle/runner.sh" <<'REMOTE'
source "$1/smoke.sh"
remote_args "$@"
exec 9<"$RELEASE_ROOT"
flock -s 9
verify_target
load_migrations
remote_smoke
REMOTE
  run_bundle "$bundle"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then local_smoke "$@"; fi
