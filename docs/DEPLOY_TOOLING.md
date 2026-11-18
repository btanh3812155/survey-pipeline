# deploy tooling

Three pieces, all checked in so a deploy agent never re-discovers the SSH layout:

| file | purpose |
|---|---|
| `scripts/deploy_prod.sh` | backup -> copy to prod + dev roots -> sha256 verify -> py_compile -> restart -> health -> optional smoke |
| `scripts/smoke_prod.sh` | read-only live checks (health, forms API, admin auth, admin host TLS, optional pytest) |
| `Makefile` | thin wrappers: `deploy`, `deploy-smoke`, `smoke`, `test` |

## deploy_prod.sh

```
scripts/deploy_prod.sh [--dry-run] [--no-restart] [--restart svc1,svc2]
                       [--smoke] [--skip-compile] [--all-changed] <relative paths...>
```

Paths are relative to the repo root; absolute paths and paths outside the repo are refused.
At least one path is required unless `--all-changed` is used (git-changed tracked files plus
untracked files under the bind-mounted dirs).

What it does, in order:

1. timestamped remote backup at `/root/deploy-backup-<UTC timestamp>/{prod,dev}/<relpath>` (existing files only)
2. streams a tar of the selected files to `/root/survey-pipeline` and `/root/survey-pipeline-dev`
   (no rsync needed; modes preserved, parent dirs created)
3. compares local vs remote `sha256sum` for every file, aborts on any mismatch
4. `python -m py_compile` for `.py` files inside the app container (`/app/<rel>`),
   falling back to the host `.venv/bin/python` when the container or file is not reachable
5. restarts the selected containers (default: app, dev, outbox), then polls the health URL for up to 90s
6. runs `scripts/smoke_prod.sh` when `--smoke` is passed

`--dry-run` prints the plan (files, sizes, hashes, roots, restart list) and touches nothing.
On success it prints a one-screen summary plus a machine-readable line:

```
DEPLOY_RESULT={"ok":true,"dry_run":false,"files":["main.py"],"backup":"/root/deploy-backup-...","roots":[...],"compile":"ok","restarted":[...],"health":"ok","smoke":"skipped"}
```

`--restart` accepts aliases (`app`, `dev`, `outbox`, `postgres`) or literal container names.

### environment overrides

| var | default |
|---|---|
| `PROD_HOST` | `root@146.190.201.32` |
| `PROD_ROOT` | `/root/survey-pipeline` |
| `DEV_ROOT` | `/root/survey-pipeline-dev` |
| `CONTAINERS` | `survey-pipeline-app-1,survey-dev-app-1,survey-pipeline-outbox-worker-1` |
| `HEALTH_URL` | `https://survey.trustbridgevn.com/health` |
| `APP_CONTAINER` | `survey-pipeline-app-1` |
| `DEV_APP_CONTAINER` | `survey-dev-app-1` |
| `HEALTH_TIMEOUT` | `90` (seconds) |

## smoke_prod.sh

```
scripts/smoke_prod.sh [--base-url URL] [--quiet] [--deep]
```

Checks: `/health` 200, `/api/v1/forms` JSON + count, `/admin/forms` `/admin/ctvs`
`/admin/image-objects` with Basic auth (only when `ADMIN_PASS` is set, otherwise SKIP),
`/api/v1/image-objects` auth + object/engine counts (SKIP on 404), admin host
`https://admin.trustbridgevn.com/admin/forms` TLS validity + auth 200, and with `--deep`
a local pytest run of the image-object/form/conditional-render tests. Exits nonzero on any FAIL
and ends with `SMOKE_RESULT={...}`.

`--quiet` suppresses per-check progress; the table and JSON line are still printed.

## examples

```bash
make deploy FILES="routers/admin_forms.py static/admin.js"
make deploy FILES=--all-changed                 # everything git says changed
make deploy-smoke FILES="main.py config.py"
scripts/deploy_prod.sh --dry-run routers/admin_forms.py
scripts/deploy_prod.sh --restart app --smoke services/vision_ai.py
ADMIN_PASS=... scripts/smoke_prod.sh --deep
```

## credentials

`ADMIN_PASS` must come from the environment (shell, password manager, CI secret). Never put the
real admin password in git, in the Makefile, in scripts, or in commit messages - it is read from
the process environment only.
