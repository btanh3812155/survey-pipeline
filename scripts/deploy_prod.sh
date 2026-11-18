#!/usr/bin/env bash
# Deploy selected repo files to the production and dev roots on the prod host.
# Backup -> copy (tar over ssh) -> sha256 verify -> py_compile -> restart -> health -> optional smoke.
#
# Usage: scripts/deploy_prod.sh [options] <relative paths...>
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PROD_HOST="${PROD_HOST:-root@146.190.201.32}"
PROD_ROOT="${PROD_ROOT:-/root/survey-pipeline}"
DEV_ROOT="${DEV_ROOT:-/root/survey-pipeline-dev}"
CONTAINERS="${CONTAINERS:-survey-pipeline-app-1,survey-dev-app-1,survey-pipeline-outbox-worker-1}"
HEALTH_URL="${HEALTH_URL:-https://survey.trustbridgevn.com/health}"
APP_CONTAINER="${APP_CONTAINER:-survey-pipeline-app-1}"
DEV_APP_CONTAINER="${DEV_APP_CONTAINER:-survey-dev-app-1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)

DRY_RUN=0 NO_RESTART=0 RUN_SMOKE=0 SKIP_COMPILE=0 ALL_CHANGED=0
RESTART_SVCS=""
declare -a FILES=()

usage() {
  cat <<'EOF'
usage: scripts/deploy_prod.sh [--dry-run] [--no-restart] [--restart svc1,svc2]
                              [--smoke] [--skip-compile] [--all-changed] <relative paths...>

  --dry-run       print the plan (files, hashes, targets, restart list) and exit
  --no-restart    copy/compile only, skip container restarts and health wait
  --restart L     comma list of services/containers to restart (app, dev, outbox,
                  postgres, or a literal container name). Default: $CONTAINERS
  --smoke         run scripts/smoke_prod.sh after a successful deploy
  --skip-compile  do not py_compile deployed .py files
  --all-changed   deploy git-changed (tracked + untracked) files under bind-mounted paths

env: PROD_HOST, PROD_ROOT, DEV_ROOT, CONTAINERS, HEALTH_URL, APP_CONTAINER, DEV_APP_CONTAINER
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

json_str() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\t'/\\t}
  printf '"%s"' "$s"
}

json_arr() { # values...
  local first=1 v out="["
  for v in "$@"; do
    (( first )) || out+=","
    out+="$(json_str "$v")"; first=0
  done
  printf '%s]' "$out"
}

emit_result() { # ok dryrun compile result health smoke error
  local files_json restarted_json
  files_json="$(json_arr "${FILES[@]}")"
  if (( ${#RESTARTED[@]} )); then restarted_json="$(json_arr "${RESTARTED[@]}")"; else restarted_json='[]'; fi
  printf 'DEPLOY_RESULT={"ok":%s,"dry_run":%s,"files":%s,"backup":%s,"roots":%s,"compile":%s,"restarted":%s,"health":%s,"smoke":%s%s}\n' \
    "$1" "$([ "$2" = 1 ] && echo true || echo false)" "$files_json" \
    "$(json_str "${BACKUP:-}")" "$(json_arr "$PROD_ROOT" "$DEV_ROOT")" \
    "$(json_str "$3")" "$restarted_json" "$(json_str "$4")" "$(json_str "$5")" \
    "${6:+,\"error\":$(json_str "$6")}"
}

# ---------------------------------------------------------------- arg parsing
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --smoke) RUN_SMOKE=1 ;;
    --skip-compile) SKIP_COMPILE=1 ;;
    --all-changed) ALL_CHANGED=1 ;;
    --restart) [[ $# -ge 2 ]] || die "--restart needs a value"; RESTART_SVCS="$2"; shift ;;
    --restart=*) RESTART_SVCS="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    --) shift; FILES+=("$@"); break ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *) FILES+=("$1") ;;
  esac
  shift
done

is_deployable() {
  case "$1" in
    main.py|config.py|requirements.txt|Caddyfile|schema.sql|Dockerfile|docker-compose.yml) return 0 ;;
    core/*|routers/*|services/*|scripts/*|static/*|workers/*|data/*|frontend/*) return 0 ;;
  esac
  return 1
}

if (( ALL_CHANGED )); then
  # Tracked changes vs HEAD (staged + unstaged) plus untracked, filtered to bind-mounted paths.
  while IFS= read -r f; do
    [[ -n "$f" && -f "$f" ]] || continue
    is_deployable "$f" && FILES+=("$f")
  done < <(git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard)
fi

(( ${#FILES[@]} )) && : || { usage; echo "ERROR: no files given" >&2; exit 2; }

# ---------------------------------------------------------------- validation
declare -a CLEAN=()
declare -A SEEN=()
for f in "${FILES[@]}"; do
  [[ "$f" != /* ]] || die "absolute paths are refused: $f"
  [[ -f "$f" ]] || die "not a regular file: $f"
  abs="$(realpath -- "$f")"
  [[ "$abs" == "$REPO_ROOT"/* ]] || die "outside repo: $f"
  rel="${abs#"$REPO_ROOT"/}"
  [[ -z "${SEEN[$rel]:-}" ]] || continue
  SEEN[$rel]=1
  CLEAN+=("$rel")
done
FILES=("${CLEAN[@]}")

declare -A LOCAL_SHA=()
for f in "${FILES[@]}"; do LOCAL_SHA[$f]="$(sha256sum -- "$f" | cut -d' ' -f1)"; done

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$(dirname "$PROD_ROOT")/deploy-backup-$TS"
declare -a RESTARTED=()

container_for_root() {
  if [[ "$1" == "$DEV_ROOT" ]]; then printf '%s' "$DEV_APP_CONTAINER"; else printf '%s' "$APP_CONTAINER"; fi
}

resolve_service() {
  case "$1" in
    app) printf '%s' "$APP_CONTAINER" ;;
    dev|app-dev) printf '%s' "$DEV_APP_CONTAINER" ;;
    outbox) printf '%s' "survey-pipeline-outbox-worker-1" ;;
    postgres) printf '%s' "survey-pipeline-postgres-1" ;;
    *) printf '%s' "$1" ;;
  esac
}

declare -a RESTART_LIST=()
if (( NO_RESTART )); then
  :
elif [[ -n "$RESTART_SVCS" ]]; then
  IFS=',' read -ra raw <<<"$RESTART_SVCS"
  for s in "${raw[@]}"; do [[ -n "$s" ]] && RESTART_LIST+=("$(resolve_service "$s")"); done
else
  IFS=',' read -ra raw <<<"$CONTAINERS"
  for s in "${raw[@]}"; do [[ -n "$s" ]] && RESTART_LIST+=("$s"); done
fi

# ---------------------------------------------------------------- dry run
if (( DRY_RUN )); then
  echo "PLAN (dry run - nothing copied, nothing restarted)"
  echo "  host:        $PROD_HOST"
  echo "  roots:       $PROD_ROOT"
  echo "               $DEV_ROOT"
  echo "  backup:      $BACKUP/{prod,dev}/<relpath>"
  echo "  container:   $(container_for_root "$PROD_ROOT") / $(container_for_root "$DEV_ROOT")"
  echo "  compile:     $([ "$SKIP_COMPILE" = 1 ] && echo skipped || echo 'python -m py_compile for .py files')"
  echo "  restart:     $([ "$NO_RESTART" = 1 ] && echo skipped || printf '%s' "${RESTART_LIST[*]:-none}")"
  echo "  health:      $HEALTH_URL (<= ${HEALTH_TIMEOUT}s)"
  echo "  smoke:       $([ "$RUN_SMOKE" = 1 ] && echo yes || echo no)"
  echo "  files (${#FILES[@]}):"
  for f in "${FILES[@]}"; do
    printf '    %-52s %8s  %s\n' "$f" "$(wc -c <"$f")" "${LOCAL_SHA[$f]:0:12}"
  done
  emit_result true 1 "$([ "$SKIP_COMPILE" = 1 ] && echo skipped || echo planned)" \
    "$([ "$NO_RESTART" = 1 ] && echo skipped || echo planned)" \
    "$([ "$RUN_SMOKE" = 1 ] && echo planned || echo skipped)"
  exit 0
fi

# ---------------------------------------------------------------- remote setup
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
printf '%s\n' "${FILES[@]}" >"$TMP_DIR/files.txt"

cat >"$TMP_DIR/prepare.sh" <<'REMOTE'
set -euo pipefail
root=$1; backup=$2; label=$3; shift 3
[ -d "$root" ] || { echo "missing root: $root" >&2; exit 3; }
mkdir -p "$backup/$label"
for rel in "$@"; do
  src="$root/$rel"
  if [ -e "$src" ]; then
    mkdir -p "$backup/$label/$(dirname "$rel")"
    cp -a "$src" "$backup/$label/$rel"
  fi
  mkdir -p "$root/$(dirname "$rel")"
done
REMOTE

cat >"$TMP_DIR/verify.sh" <<'REMOTE'
set -euo pipefail
root=$1; shift
for rel in "$@"; do
  [ -f "$root/$rel" ] || { echo "MISSING $rel" >&2; exit 4; }
  sha256sum "$root/$rel"
done
REMOTE

cat >"$TMP_DIR/compile.sh" <<'REMOTE'
set -euo pipefail
root=$1; container=$2; shift 2
pys=()
for rel in "$@"; do [ "${rel##*.}" = py ] && pys+=("$rel"); done
[ ${#pys[@]} -gt 0 ] || { echo "no-python"; exit 0; }
fallback() {
  if [ -x "$root/.venv/bin/python" ]; then "$root/.venv/bin/python" -m py_compile "$root/$1"
  elif command -v python3 >/dev/null 2>&1; then python3 -m py_compile "$root/$1"
  else echo "no python available to compile $1" >&2; exit 5
  fi
}
running=0
if command -v docker >/dev/null 2>&1 && [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" = "true" ]; then
  running=1
fi
for rel in "${pys[@]}"; do
  if [ "$running" = 1 ] && docker exec "$container" test -f "/app/$rel" 2>/dev/null; then
    docker exec "$container" python -m py_compile "/app/$rel"
  else
    fallback "$rel"
  fi
done
echo "compiled ${#pys[@]}"
REMOTE

cat >"$TMP_DIR/restart.sh" <<'REMOTE'
set -euo pipefail
docker restart "$@" >/dev/null
REMOTE

FAIL_LOG="$TMP_DIR/last.log"

ssh_run() { # script-file args...
  local sfile=$1; shift
  local cmd="bash -s --" a
  for a in "$@"; do cmd+=" $(printf '%q' "$a")"; done
  ssh "${SSH_OPTS[@]}" "$PROD_HOST" "$cmd" <"$sfile"
}

fail() {
  echo "DEPLOY FAILED: $*" >&2
  [[ -s "$FAIL_LOG" ]] && sed -n '1,80p' "$FAIL_LOG" >&2
  emit_result false 0 "${COMPILE_STATE:-failed}" "${HEALTH_STATE:-skipped}" "${SMOKE_STATE:-skipped}" "$*"
  exit 1
}

# ---------------------------------------------------------------- per-root work
COMPILE_STATE="skipped"
HEALTH_STATE="skipped"
SMOKE_STATE="skipped"

for root in "$PROD_ROOT" "$DEV_ROOT"; do
  label=prod; [[ "$root" == "$DEV_ROOT" ]] && label=dev
  if ! ssh_run "$TMP_DIR/prepare.sh" "$root" "$BACKUP" "$label" "${FILES[@]}" >"$FAIL_LOG" 2>&1; then
    fail "backup/mkdir failed on $root"
  fi
  if ! tar -cf - -T "$TMP_DIR/files.txt" | ssh "${SSH_OPTS[@]}" "$PROD_HOST" "cd $(printf '%q' "$root") && tar -xf -" >"$FAIL_LOG" 2>&1; then
    fail "copy failed on $root"
  fi
  if ! ssh_run "$TMP_DIR/verify.sh" "$root" "${FILES[@]}" >"$TMP_DIR/remote_sha.txt" 2>"$FAIL_LOG"; then
    fail "sha256 verify failed on $root"
  fi
  declare -A REMOTE_SHA=()
  while read -r hash path; do REMOTE_SHA["$path"]="$hash"; done <"$TMP_DIR/remote_sha.txt"
  for f in "${FILES[@]}"; do
    actual="${REMOTE_SHA["$root/$f"]:-}"
    [[ "$actual" == "${LOCAL_SHA[$f]}" ]] || fail "sha256 mismatch for $f on $root (local ${LOCAL_SHA[$f]:0:12}, remote ${actual:0:12})"
  done
  unset REMOTE_SHA
  if (( SKIP_COMPILE )); then
    COMPILE_STATE="skipped"
  elif ! ssh_run "$TMP_DIR/compile.sh" "$root" "$(container_for_root "$root")" "${FILES[@]}" >"$TMP_DIR/compile.out" 2>"$FAIL_LOG"; then
    fail "py_compile failed on $root"
  else
    COMPILE_STATE="ok"
  fi
done

# ---------------------------------------------------------------- restart + health
if (( ! NO_RESTART )); then
  if (( ${#RESTART_LIST[@]} )); then
    if ! ssh_run "$TMP_DIR/restart.sh" "${RESTART_LIST[@]}" >"$FAIL_LOG" 2>&1; then
      fail "docker restart failed: ${RESTART_LIST[*]}"
    fi
    RESTARTED=("${RESTART_LIST[@]}")
  fi
  deadline=$((SECONDS + HEALTH_TIMEOUT))
  while :; do
    code="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || true)"
    [[ "$code" == 200 ]] && { HEALTH_STATE="ok"; break; }
    if (( SECONDS >= deadline )); then
      HEALTH_STATE="timeout"
      fail "health not 200 after ${HEALTH_TIMEOUT}s ($HEALTH_URL, last HTTP ${code:-none})"
    fi
    sleep 2
  done
fi

# ---------------------------------------------------------------- smoke
if (( RUN_SMOKE )); then
  smoke_base="${HEALTH_URL%/health}"
  if ! "$REPO_ROOT/scripts/smoke_prod.sh" --base-url "$smoke_base" >"$TMP_DIR/smoke.out" 2>&1; then
    SMOKE_STATE="failed"
    sed -n '1,80p' "$TMP_DIR/smoke.out" >&2
    fail "smoke checks failed"
  fi
  SMOKE_STATE="ok"
fi

# ---------------------------------------------------------------- summary
echo "DEPLOY OK"
echo "  files:     ${#FILES[@]} -> $PROD_ROOT, $DEV_ROOT"
echo "  backup:    $BACKUP"
echo "  compile:   $COMPILE_STATE"
echo "  restarted: $([ ${#RESTARTED[@]} -gt 0 ] && printf '%s' "${RESTARTED[*]}" || echo skipped)"
echo "  health:    $HEALTH_STATE ($HEALTH_URL)"
echo "  smoke:     $SMOKE_STATE"
emit_result true 0 "$COMPILE_STATE" "$HEALTH_STATE" "$SMOKE_STATE"
