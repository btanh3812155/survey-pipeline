#!/usr/bin/env bash
# Read-only production smoke checks for the survey pipeline.
# Usage: scripts/smoke_prod.sh [--base-url URL] [--quiet] [--deep]
#   ADMIN_PASS (and optional ADMIN_USER, default "admin") enables admin auth checks.
set -euo pipefail

BASE_URL="https://survey.trustbridgevn.com"
ADMIN_HOST_URL="${ADMIN_HOST_URL:-https://admin.trustbridgevn.com}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-}"
QUIET=0
DEEP=0

usage() {
  cat <<'EOF'
usage: scripts/smoke_prod.sh [--base-url URL] [--quiet] [--deep]

  --base-url URL   public app base URL (default https://survey.trustbridgevn.com)
  --quiet          suppress per-check progress, keep the table + SMOKE_RESULT
  --deep           additionally run the local image-object/form pytest subset

env: ADMIN_PASS (required for admin auth checks), ADMIN_USER (default admin),
     ADMIN_HOST_URL (default https://admin.trustbridgevn.com)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) [[ $# -ge 2 ]] || { echo "ERROR: --base-url needs a value" >&2; exit 2; }; BASE_URL="$2"; shift ;;
    --base-url=*) BASE_URL="${1#*=}" ;;
    --quiet) QUIET=1 ;;
    --deep) DEEP=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
BASE_URL="${BASE_URL%/}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

declare -a ROW_NAME=() ROW_STATUS=() ROW_DETAIL=()
FAILED=0
SKIPPED=0

record() { # name status detail
  ROW_NAME+=("$1"); ROW_STATUS+=("$2"); ROW_DETAIL+=("${3:-}")
  (( QUIET )) || printf '  %-6s %s\n' "$2" "$1${3:+ - $3}"
  [[ "$2" == FAIL ]] && FAILED=$((FAILED + 1)) || true
  [[ "$2" == SKIP ]] && SKIPPED=$((SKIPPED + 1)) || true
}

json_str() {
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\n'/\\n}; s=${s//$'\t'/\\t}
  printf '"%s"' "$s"
}

# fetch URL outfile -> globals FETCH_CODE / FETCH_RC; stderr in FETCH_ERR
fetch() {
  FETCH_CODE="$(curl -sS -m 25 -o "$2" -w '%{http_code}' "$1" 2>"$TMP_DIR/curl.err")" && FETCH_RC=0 || FETCH_RC=$?
  FETCH_ERR="$(tr '\n' ' ' <"$TMP_DIR/curl.err" | head -c 200)"
}

count_from() { # file key -> count of parsed JSON list, or "?" on failure
  "${PY_BIN:-python3}" - "$1" "$2" <<'PY' 2>/dev/null || echo "?"
import json, sys
data = json.load(open(sys.argv[1]))
value = data.get(sys.argv[2])
print(len(value) if isinstance(value, list) else "?")
PY
}

PY_BIN="$REPO_ROOT/.venv/bin/python"
[[ -x "$PY_BIN" ]] || PY_BIN="$(command -v python3 || true)"
BODY="$TMP_DIR/body"

# 1. /health
fetch "$BASE_URL/health" "$BODY"
if (( FETCH_RC == 0 )) && [[ "$FETCH_CODE" == 200 ]] && grep -q '"healthy"' "$BODY"; then
  record "health" PASS "HTTP 200, status healthy"
else
  record "health" FAIL "HTTP ${FETCH_CODE:-none} ${FETCH_ERR}"
fi

# 2. /api/v1/forms
fetch "$BASE_URL/api/v1/forms" "$BODY"
if (( FETCH_RC == 0 )) && [[ "$FETCH_CODE" == 200 ]]; then
  count="$(sed -n 's/.*"count":[[:space:]]*\([0-9]\+\).*/\1/p' "$BODY" | head -1)"
  if [[ -n "$count" ]]; then
    record "forms-api" PASS "HTTP 200, count=$count"
  else
    record "forms-api" FAIL "HTTP 200 but no count in payload"
  fi
else
  record "forms-api" FAIL "HTTP ${FETCH_CODE:-none} ${FETCH_ERR}"
fi

# 3. admin pages (auth only when ADMIN_PASS is available)
admin_auth() { # label path -> sets AC_CODE
  AC_CODE="$(curl -sS -m 25 -o "$BODY" -w '%{http_code}' -u "$ADMIN_USER:$ADMIN_PASS" \
    "$BASE_URL$1" 2>"$TMP_DIR/curl.err")" && AC_RC=0 || AC_RC=$?
  AC_ERR="$(tr '\n' ' ' <"$TMP_DIR/curl.err" | head -c 200)"
}
if [[ -n "$ADMIN_PASS" ]]; then
  for page in forms:/admin/forms ctvs:/admin/ctvs image-objects:/admin/image-objects; do
    label="${page%%:*}"; path="${page#*:}"
    admin_auth "$path"
    if (( AC_RC == 0 )) && [[ "$AC_CODE" == 200 ]]; then
      record "admin-$label" PASS "HTTP 200 (auth)"
    else
      record "admin-$label" FAIL "HTTP ${AC_CODE:-none} ${AC_ERR}"
    fi
  done

  # 4. image-object API auth + object/engine counts (skip if endpoint absent)
  admin_auth /api/v1/image-objects
  if [[ "$AC_CODE" == 404 ]]; then
    record "image-objects-api" SKIP "endpoint not present (404)"
  elif (( AC_RC == 0 )) && [[ "$AC_CODE" == 200 ]]; then
    objects="$(count_from "$BODY" objects)"
    engines="$(count_from "$BODY" engines)"
    record "image-objects-api" PASS "HTTP 200, objects=$objects engines=$engines"
  else
    record "image-objects-api" FAIL "HTTP ${AC_CODE:-none} ${AC_ERR}"
  fi
else
  record "admin-pages" SKIP "ADMIN_PASS unset"
  record "image-objects-api" SKIP "ADMIN_PASS unset"
fi

# 5. admin host: TLS validity always, auth only with a password
AH_CODE="$(curl -sS -m 25 -o "$BODY" -w '%{http_code}' -u "$ADMIN_USER:${ADMIN_PASS:-x}" \
  "$ADMIN_HOST_URL/admin/forms" 2>"$TMP_DIR/curl.err")" && AH_RC=0 || AH_RC=$?
AH_ERR="$(tr '\n' ' ' <"$TMP_DIR/curl.err" | head -c 200)"
if (( AH_RC != 0 )) || [[ "$AH_CODE" == 000 ]]; then
  record "admin-host-tls" FAIL "TLS/connect failed: $AH_ERR"
elif [[ -n "$ADMIN_PASS" ]] && [[ "$AH_CODE" == 200 ]]; then
  record "admin-host" PASS "HTTPS 200, valid TLS, auth ok"
elif [[ -z "$ADMIN_PASS" ]] && [[ "$AH_CODE" =~ ^(200|302|401|403)$ ]]; then
  record "admin-host" SKIP "TLS ok (HTTP $AH_CODE), auth skipped: ADMIN_PASS unset"
else
  record "admin-host" FAIL "HTTP $AH_CODE $AH_ERR"
fi

# 6. optional local deep tests
if (( DEEP )); then
  tests=(tests/test_image_objects.py tests/test_form_editor_image_objects_ui.py tests/test_conditional_editor_and_rendering.py)
  missing=0
  for t in "${tests[@]}"; do [[ -f "$REPO_ROOT/$t" ]] || { missing=1; record "deep:$t" FAIL "test file missing"; }; done
  if (( ! missing )); then
    if (cd "$REPO_ROOT" && "$PY_BIN" -m pytest "${tests[@]}" -q >"$TMP_DIR/pytest.out" 2>&1); then
      record "deep-pytest" PASS "$(tail -1 "$TMP_DIR/pytest.out")"
    else
      record "deep-pytest" FAIL "$(tail -1 "$TMP_DIR/pytest.out")"
    fi
  fi
fi

# ---------------------------------------------------------------- summary
name_w=0
for n in "${ROW_NAME[@]}"; do (( ${#n} > name_w )) && name_w=${#n}; done
echo
printf '%-*s %-5s %s\n' "$name_w" "CHECK" "STATE" "DETAIL"
for i in "${!ROW_NAME[@]}"; do
  printf '%-*s %-5s %s\n' "$name_w" "${ROW_NAME[$i]}" "${ROW_STATUS[$i]}" "${ROW_DETAIL[$i]}"
done

checks=${#ROW_NAME[@]}
ok=$(( FAILED ? 0 : 1 ))
printf 'SMOKE_RESULT={"ok":%s,"base_url":%s,"checks":%d,"failed":%d,"skipped":%d,"deep":%s,"failures":[' \
  "$([ "$ok" = 1 ] && echo true || echo false)" "$(json_str "$BASE_URL")" \
  "$checks" "$FAILED" "$SKIPPED" "$([ "$DEEP" = 1 ] && echo true || echo false)"
first=1
for i in "${!ROW_NAME[@]}"; do
  [[ "${ROW_STATUS[$i]}" == FAIL ]] || continue
  (( first )) || printf ','
  printf '%s' "$(json_str "${ROW_NAME[$i]}")"
  first=0
done
printf ']}\n'

(( FAILED == 0 ))
