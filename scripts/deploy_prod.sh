#!/usr/bin/env bash
# Publish bind-mounted code only. Infrastructure, configuration and data are not releases.
set -euo pipefail
REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$REPO_ROOT/scripts/smoke_prod.sh"
cd "$REPO_ROOT"

usage() {
  cat <<'EOF'
usage: deploy_prod.sh --target production|staging --schema 002 [--dry-run]
                      [--apply-migrations] (--all-code | path ...)
       deploy_prod.sh --target production|staging --schema 002 [--dry-run]
                      --rollback release-ID

Set RELEASE_HOST (pinned SSH host), RELEASE_ROOT, RELEASE_APP, RELEASE_WORKER,
RELEASE_DB_CONTAINER, RELEASE_DB_NAME and RELEASE_BUILD_ID explicitly. The app
and worker must both be provisioned with that APP_BUILD_ID. RELEASE_DB_USER
defaults to postgres; RELEASE_TIMEOUT controls probe waits (90 seconds).
Provision the target app and worker out of band with unless-stopped restart
policies, the same PostgreSQL database, and a shared private data bind mount.
The worker needs OUTBOX_WORKER_ID, OUTBOX_V2_WORKER_ENABLED=true, and
DRIVE_SYNC_INTERVAL_SEC=0.
Schema 002 means queue format 002 plus the required operations migration 003.
Use --apply-migrations only after approval. Pending migrations require a target
backup before the first migration; rollback restores selected code only and
leaves the app and worker paused. No producer settings are changed.
EOF
}

TARGET= SCHEMA= ALL_CODE=0 DRY_RUN=0 APPLY=0 ROLLBACK=
declare -a requested=()
while (( $# )); do
  case "$1" in
    --target) TARGET=${2:?missing target}; shift ;;
    --schema) SCHEMA=${2:?missing schema}; shift ;;
    --all-code) ALL_CODE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --apply-migrations) APPLY=1 ;;
    --rollback) ROLLBACK=${2:?missing release ID}; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) requested+=("$1") ;;
  esac
  shift
done
release_args

# The workflow uses this exact allowlist, not a separate rsync filter. Only
# tracked, flat code paths and versioned migrations qualify; never send a tree.
allow_code() {
  case "$1" in
    *credential*|*secret*|*service_account*|*private_key*|*devfile*|*backup*|*token*) return 1 ;;
  esac
  [[ $1 =~ ^(main\.py|config\.py|(core|routers|services|workers)/[a-z_][a-z0-9_]*\.py|static/(ui/[a-z_][a-z0-9_]*\.html|qrcode\.min\.js)|scripts/migrations/(001_submission_attempts|002_outbox_delivery|003_outbox_operations)\.sql)$ ]]
}

if [[ -n "$ROLLBACK" ]]; then
  [[ "$ROLLBACK" =~ ^release-[a-zA-Z0-9]{8}$ ]] || die 'invalid rollback release ID'
  (( ! ALL_CODE && ! APPLY && ${#requested[@]} == 0 )) || die 'rollback cannot publish code or apply migrations'
elif (( ALL_CODE )); then
  (( ${#requested[@]} == 0 )) || die 'choose either --all-code or explicit paths'
  tracked=$(mktemp)
  trap 'rm -f -- "$tracked"' EXIT
  git ls-files -z >"$tracked"
  while IFS= read -r -d '' path; do
    if allow_code "$path"; then requested+=("$path"); fi
  done <"$tracked"
  rm -f -- "$tracked"
  trap - EXIT
else
  (( ${#requested[@]} )) || die 'choose --all-code or explicit paths'
fi

bundle=$(mktemp -d)
trap 'rm -rf -- "$bundle"' EXIT
bundle_base "$bundle"
mkdir "$bundle/code"
: >"$bundle/files"
: >"$bundle/sha256"
declare -A seen=()
for path in "${requested[@]}"; do
  allow_code "$path" || die "not an immutable release path: $path"
  git ls-files --error-unmatch -- "$path" >/dev/null 2>&1 || die "not a tracked release file: $path"
  [[ -f "$path" && $(realpath -e -- "$path") == "$REPO_ROOT/$path" ]] || die "not a regular, symlink-free release file: $path"
  [[ ! ${seen[$path]+selected} ]] || continue
  seen[$path]=1
  printf '%s\n' "$path" >>"$bundle/files"
  cp --parents -p -- "$path" "$bundle/code"
  (cd "$bundle/code" && sha256sum -- "$path") >>"$bundle/sha256"
done
if [[ -z "$ROLLBACK" ]]; then [[ -s "$bundle/files" ]] || die 'no immutable release files found'; fi
if (( DRY_RUN )); then
  printf 'PLAN (local only) target=%s host=%s root=%s schema=%s build=%s apply=%s rollback=%s\n' "$TARGET" "$RELEASE_HOST" "$RELEASE_ROOT" "$SCHEMA" "$RELEASE_BUILD_ID" "$APPLY" "${ROLLBACK:-none}"
  cat "$bundle/sha256"
  exit 0
fi

{
  declare -f allow_code
  cat <<'REMOTE'
source "$1/smoke.sh"
remote_args "$@"
apply=${13} rollback=${14}
exec 9<"$RELEASE_ROOT"
flock -x 9

backups="$RELEASE_ROOT/.release-backups"
files="$BUNDLE/files"
if [[ -n "$rollback" ]]; then
  backup="$backups/$rollback"
  [[ -d "$backup" && $(realpath -e -- "$backup") == "$backup" ]] || die 'unknown or unsafe rollback backup'
  files="$backup/files"
fi

safe_paths() {
  local rel dst parent
  [[ -f "$files" && ! -L "$files" ]] || return 1
  while IFS= read -r rel; do
    allow_code "$rel" || return 1
    dst="$RELEASE_ROOT/$rel"
    [[ ! -L "$dst" && ( ! -e "$dst" || -f "$dst" ) ]] || return 1
    parent=${dst%/*}
    while [[ "$parent" != "$RELEASE_ROOT" ]]; do
      [[ ! -L "$parent" && ( ! -e "$parent" || -d "$parent" ) ]] || return 1
      parent=${parent%/*}
    done
  done <"$files"
}

pause_services() {
  docker stop "$APP_ID" "$WORKER_ID" >/dev/null
}

replace_code() {
  local source=$1 destination=$2 temp
  (umask 022; mkdir -p -- "${destination%/*}") || return 1
  temp=$(mktemp -- "${destination%/*}/.release-file.XXXXXXXX") || return 1
  if ! cp -p -- "$source" "$temp" || ! mv -fT -- "$temp" "$destination"; then
    rm -f -- "$temp"
    return 1
  fi
}

restore_code() {
  local rel src dst
  safe_paths || return 1
  while IFS= read -r rel; do
    src="$backup/prior/$rel" dst="$RELEASE_ROOT/$rel"
    if [[ $(realpath -m -- "$src") != "$src" ]]; then return 1; fi
    if [[ -f "$src" ]]; then
      replace_code "$src" "$dst" || return 1
    elif [[ ! -e "$src" ]]; then
      rm -f -- "$dst" || return 1
    else
      return 1
    fi
  done <"$files"
}

safe_paths || die 'unsafe code destination or manifest (symlink, directory, or protected path)'
verify_target "$files"
load_migrations
check_ledger
identity="$TARGET|$RELEASE_ROOT|$RELEASE_DB_NAME|$APP_ID|$WORKER_ID|$DB_ID"

if [[ -n "$rollback" ]]; then
  [[ -f "$backup/identity" && ! -L "$backup/identity" && $(<"$backup/identity") == "$identity" ]] || die 'snapshot belongs to a different target'
  pause_services
  restore_code || die "code restoration incomplete; services are paused, see $backup"
  printf 'ROLLBACK: restored selected code from %s; app and worker remain paused; database and runtime state retained\n' "$backup"
  exit 0
fi

pending=$((${#EXPECTED_LEDGER[@]} - ${#LEDGER[@]}))
(( ! pending || apply )) || die 'missing required migrations; approve --apply-migrations first'
(cd "$BUNDLE/code" && sha256sum --check --status "$BUNDLE/sha256") || die 'release checksum mismatch'
python3 - "$BUNDLE/code" <<'PY'
from pathlib import Path
import sys

for path in Path(sys.argv[1]).rglob("*.py"):
    compile(path.read_bytes(), str(path), "exec")
PY

[[ ! -L "$backups" && ( ! -e "$backups" || -d "$backups" ) ]] || die 'unsafe backup directory'
mkdir -p -- "$backups"
backup=$(mktemp -d -- "$backups/release-XXXXXXXX")
mkdir -- "$backup/prior"
while IFS= read -r rel; do
  if [[ -f "$RELEASE_ROOT/$rel" ]]; then
    (cd "$RELEASE_ROOT" && cp --parents -p -- "$rel" "$backup/prior")
  fi
done <"$files"
cp -- "$files" "$backup/files"
cp -- "$BUNDLE/sha256" "$backup/release.sha256"
printf '%s\n' "$identity" >"$backup/identity"
printf 'RELEASE_BACKUP=%s\n' "$backup"

published=0
recover() {
  local rc=$?
  trap - EXIT HUP INT TERM
  if (( rc )); then
    if pause_services && { (( ! published )) || restore_code; }; then
      printf 'RELEASE FAILED: code restored or unchanged; app and worker paused; schema and runtime state retained (%s)\n' "$backup" >&2
    else
      printf 'RELEASE FAILED: automatic pause/restoration incomplete; verify services and inspect %s\n' "$backup" >&2
    fi
  fi
  exit "$rc"
}
trap recover EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

pause_services
if (( pending )); then
  target_pg read pg_dump -Fc </dev/null >"$backup/database.dump.partial"
  [[ -s "$backup/database.dump.partial" ]] || die 'database backup is empty'
  mv -- "$backup/database.dump.partial" "$backup/database.dump"
  for i in "${!MIGRATIONS[@]}"; do
    (( i >= ${#LEDGER[@]} )) || continue
    printf 'Applying migration %s to the verified target\n' "${EXPECTED_LEDGER[i]%%|*}"
    { guard_sql; cat -- "${MIGRATIONS[i]}"; } | target_sql write \
      -v "migration_sha256=${EXPECTED_LEDGER[i]#*|}" \
      -v "migration_001_sha256=${EXPECTED_LEDGER[0]#*|}" \
      -v "migration_002_sha256=${EXPECTED_LEDGER[1]#*|}" >/dev/null
    check_ledger
    (( ${#LEDGER[@]} > i )) || die 'migration did not record the expected checksum'
  done
fi
require_schema

published=1
while IFS= read -r rel; do
  replace_code "$BUNDLE/code/$rel" "$RELEASE_ROOT/$rel"
done <"$files"
(cd "$RELEASE_ROOT" && sha256sum --check --status "$BUNDLE/sha256") || die 'installed code checksum mismatch'

docker start "$WORKER_ID" >/dev/null
wait_probe worker_probe
docker start "$APP_ID" >/dev/null
wait_probe app_probe
remote_smoke
printf 'DEPLOY_RESULT={"ok":true,"target":"%s","schema":"%s","build_id":"%s","backup":"%s"}\n' "$TARGET" "$SCHEMA" "$RELEASE_BUILD_ID" "${backup##*/}"
REMOTE
} >"$bundle/runner.sh"
run_bundle "$bundle" "$APPLY" "$ROLLBACK"
