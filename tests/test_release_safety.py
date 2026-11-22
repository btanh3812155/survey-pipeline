"""Exercise the production scripts with local SSH/Docker substitutes only."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "001_submission_attempts.sql",
    "002_outbox_delivery.sql",
    "003_outbox_operations.sql",
    "004_image_dinov2_exceptions.sql",
    "005_image_dhash_exceptions.sql",
    "006_admin_accounts.sql",
    "007_admin_account_bin.sql",
    "008_admin_module_registry.sql",
    "009_drive_exports.sql",
)
TARGET = ("--target", "production", "--schema", "002")
RUNTIME = (
    ".env", "service_account.json", "token.json", "credentials.json", "docker-compose.yml", "core/credentials.py",
    "models/vision.onnx", "archives/backup.tar", "devfiles/replay.json",
    "data/forms/live.json", "data/forms/placeholder_live.json", "data/form_backups/old.json",
    "data/submission_photos/submission/revision.webp", "data/media/blob", "data/dev_submissions.json",
    "data/image_objects/registry.json", "data/check_engines/registry.json", "data/drive_mirror/export.csv",
    "static/uploads/image.html", "scripts/migrations/operator-backup.sql",
)

FAKE_DOCKER = r'''
import datetime, http.client, json, os, pathlib, sys

env = os.environ
args = sys.argv[1:]
assert args[:2] == ['--host', 'unix:///var/run/docker.sock'], args
args = args[2:]
root = pathlib.Path(env['RELEASE_ROOT'])
state = pathlib.Path(env['TEST_STATE'])
ledger = state / 'ledger'

def event(text):
    with (state / 'events').open('a') as stream:
        stream.write(text + '\n')

if args[:2] == ['inspect', '--type']:
    assert args[2:] == ['container', 'app', 'worker', 'postgres']
    event('inspect')
    network = {'NetworkID': 'fixture-network', 'IPAddress': '172.18.0.3', 'Aliases': ['postgres']}
    db = {'Id': 'db-id', 'Name': '/postgres', 'State': {'Running': True},
          'NetworkSettings': {'Networks': {'testnet': network}}}
    def service(name):
        mounts = [{'Destination': '/app/' + path, 'Source': str(root / path), 'Type': 'bind', 'RW': True}
                  for path in ('data', 'core', 'main.py', 'config.py', 'scripts')]
        configured_env = env.get('FAKE_ENVIRONMENT', 'production')
        configured_db = env.get('FAKE_DATABASE', 'surveydb')
        if name == 'worker' and env.get('WRONG_WORKER_MOUNT'):
            mounts[0]['Source'] = str(root.parent / 'staging/data')
        return {'Id': name + '-id', 'Name': '/' + name, 'Mounts': mounts,
                'Config': {'WorkingDir': '/app', 'Entrypoint': None, 'Cmd': ['python', '-m', 'workers.outbox_worker'],
                           'Env': ['ENVIRONMENT=' + configured_env,
                                   'DATABASE_URL=postgresql://postgres:never-print@postgres:5432/' + configured_db,
                                   'APP_BUILD_ID=' + env.get('CONFIGURED_BUILD', 'fixture-build'),
                                   'OUTBOX_WORKER_ID=fixture-worker', 'OUTBOX_V2_WORKER_ENABLED=true',
                                   'DRIVE_SYNC_INTERVAL_SEC=0']},
                'NetworkSettings': {'Networks': {'testnet': network}},
                'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}}}
    print(json.dumps([service('app'), service('worker'), db]))
elif args == ['inspect', '-f', '{{.State.StartedAt}}', 'worker-id']:
    print((state / 'started').read_text())
elif args[0] == 'stop':
    assert args[1:] == ['app-id', 'worker-id'], args
    event('pause')
elif args[0] == 'start':
    assert len(args) == 2 and args[1] in ('app-id', 'worker-id'), args
    assert len(ledger.read_text().splitlines()) == 9
    assert (root / 'main.py').read_text() == 'RELEASE = "new"\n'
    event('start ' + args[1])
    if args[1] == 'worker-id':
        (state / 'started').write_text(datetime.datetime.now(datetime.timezone.utc).isoformat())
elif args[:3] == ['exec', '-i', 'db-id']:
    assert args[3:10] == ['env', '-u', 'PGHOSTADDR', '-u', 'PGSERVICE', '-u', 'PGDATABASE'], args
    opts, program = args[10:12]
    assert 'search_path=public' in opts
    assert args[12:21] == ['-w', '-h', '/var/run/postgresql', '-p', '5432', '-U', 'postgres', '-d', 'surveydb'], args
    if program == 'pg_dump':
        assert args[21:] == ['-Fc'] and 'default_transaction_read_only=on' in opts
        event('backup')
        print('disposable logical backup')
        sys.exit(0)
    assert program == 'psql' and args[21:27] == ['-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-v', 'release_database=surveydb']
    sql = sys.stdin.read()
    assert "current_database() = :'release_database'" in sql and "current_schema() = 'public'" in sql
    if env.get('FAKE_SQL_TARGET_REFUSED'):
        event('sql target refused')
        sys.exit(1)
    if len(args) > 27:
        assert 'default_transaction_read_only=on' not in opts
        version = str(len(ledger.read_text().splitlines()) + 1).zfill(3)
        event('migrate ' + version)
        assert (root / 'main.py').read_text() == 'RELEASE = "old"\n'
        assert version in sql and [part for part in args if part.startswith('migration_sha256=')]
        if env.get('FAIL_MIGRATION') == version:
            sys.exit(1)
        digest = next(part.split('=', 1)[1] for part in args if part.startswith('migration_sha256='))
        with ledger.open('a') as stream:
            stream.write(version + '|' + digest + '\n')
    else:
        assert 'default_transaction_read_only=on' in opts
        if 'FROM public.schema_migrations' in sql:
            event('ledger ' + str(len(ledger.read_text().splitlines())))
            print(ledger.read_text(), end='')
        else:
            assert 'FROM public.outbox_events' in sql and 'DELIVERY_UNCERTAIN' in sql
            assert 'oldest_uncertain_age_seconds' in sql and 'oldest_failed_age_seconds' in sql
            event('queue read')
            print(json.dumps({'failed': 2, 'uncertain': 1, 'oldest_failed_age_seconds': 42}))
elif args[:7] == ['exec', '-w', '/app', '-e', 'PGCONNECT_TIMEOUT=5', '-e',
                  'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=5000']:
    assert args[7:] == ['worker-id', 'python', '-B', '-m', 'workers.outbox_worker', '--health']
    event('worker health')
    print(json.dumps({'ready': True, 'schema_version': '002', 'health_schema_version': '003',
                      'build_id': env.get('HEARTBEAT_BUILD', 'fixture-build'),
                      'worker_id': 'fixture-worker', 'heartbeat_age_seconds': json.loads(env.get('HEARTBEAT', '0')),
                      'supported_targets': ['BILLING', 'R2_UPLOAD', 'GDRIVE_EXPORT', 'LARK_SYNC', 'TELEGRAM_NOTIFY']}))
elif args[:5] == ['exec', 'app-id', 'python', '-B', '-c']:
    class Response:
        def __init__(self, path):
            self.path = path
            self.status = 503 if env.get('FAIL_READY') and path == '/ready' else 200
        def read(self, size):
            return json.dumps({'status': {'/health': 'healthy', '/ready': 'ready'}[self.path],
                               'environment': 'production'}).encode()
    class HTTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ('127.0.0.1', 8000)
        def request(self, method, path):
            assert method == 'GET' and path in ('/health', '/ready')
            event('GET ' + path)
            self.path = path
        def getresponse(self):
            return Response(self.path)
        def close(self):
            pass
    http.client.HTTPConnection = HTTP
    sys.argv = ['-c', *args[6:]]
    exec(args[5])
else:
    raise AssertionError(args)
'''


@pytest.fixture
def release(tmp_path):
    checkout, target, staging, bin_dir, state = (
        tmp_path / name for name in ("checkout", "production", "staging", "bin", "state")
    )
    for directory in (checkout, target, staging, bin_dir, state):
        directory.mkdir()

    def put(base, relative, content):
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    for name in ("deploy_prod.sh", "smoke_prod.sh", *(f"migrations/{m}" for m in MIGRATIONS)):
        relative = Path("scripts") / name
        (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, checkout / relative)
    put(checkout, "main.py", 'RELEASE = "new"\n')
    put(checkout, "core/extra.py", 'NEW_CODE = True\n')
    put(target, "main.py", 'RELEASE = "old"\n')
    put(target, "core/unlisted.py", 'UNCHANGED = True\n')
    put(staging, "main.py", 'RELEASE = "untouched staging"\n')
    for path in RUNTIME:
        put(checkout, path, "repository copy must never be published")
        put(target, path, f"live {path}")
    os.link(target / "main.py", target / "data/original-hardlink")

    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "--", "."], check=True)
    put(checkout, "core/untracked.py", 'UNTRACKED = True\n')
    put(bin_dir, "ssh", """#!/usr/bin/env bash
set -eu
[[ $* == *'StrictHostKeyChecking=yes'* && ${@: -2:1} == deploy@release.invalid ]]
printf 'ssh fixture\\n' >> "$TEST_STATE/events"
exec bash -c "${@: -1}"
""")
    put(bin_dir, "docker", f"#!{sys.executable}\n{FAKE_DOCKER}")
    for name in ("ssh", "docker"):
        (bin_dir / name).chmod(0o700)
    put(state, "ledger", "")
    put(state, "events", "")
    put(state, "started", "2025-01-01T00:00:00+00:00")
    expected_ledger = [
        f"{name[:3]}|{hashlib.sha256((checkout / 'scripts/migrations' / name).read_bytes()).hexdigest()}"
        for name in MIGRATIONS
    ]
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_STATE=str(state),
               RELEASE_HOST="deploy@release.invalid", RELEASE_ROOT=str(target), RELEASE_APP="app",
               RELEASE_WORKER="worker", RELEASE_DB_CONTAINER="postgres", RELEASE_DB_NAME="surveydb",
               RELEASE_DB_USER="postgres", RELEASE_TIMEOUT="1", RELEASE_BUILD_ID="fixture-build")

    def run(*flags, script="deploy_prod.sh", **overrides):
        return subprocess.run(["bash", str(checkout / "scripts" / script), *flags],
                              env=dict(env, **overrides), text=True, capture_output=True, timeout=15)

    return run, target, staging, state, expected_ledger


def assert_runtime(target, staging):
    for path in RUNTIME:
        assert (target / path).read_text() == f"live {path}"
    assert (target / "data/original-hardlink").read_text() == 'RELEASE = "old"\n'
    assert (target / "core/unlisted.py").read_text() == 'UNCHANGED = True\n'
    assert not (target / "core/untracked.py").exists()
    assert (staging / "main.py").read_text() == 'RELEASE = "untouched staging"\n'


def test_manual_path_verifies_target_migrates_before_start_and_can_roll_back(release):
    run, target, staging, state, ledger = release
    result = run(*TARGET, "--all-code", "--apply-migrations")
    assert result.returncode == 0, result.stderr
    assert_runtime(target, staging)
    assert (target / "core/extra.py").exists()
    assert (state / "ledger").read_text().splitlines() == ledger
    events = (state / "events").read_text().splitlines()
    assert events == ["ssh fixture", "inspect", "ledger 0", "pause", "backup", "migrate 001",
                      "ledger 1", "migrate 002", "ledger 2", "migrate 003", "ledger 3",
                      "migrate 004", "ledger 4", "migrate 005", "ledger 5", "migrate 006", "ledger 6",
                      "migrate 007", "ledger 7", "migrate 008", "ledger 8", "migrate 009", "ledger 9",
                      "ledger 9", "start worker-id", "worker health",
                      "start app-id", "GET /health", "GET /ready", "ledger 9", "worker health",
                      "GET /health", "GET /ready", "queue read"]
    outcome = json.loads(next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                              if line.startswith("DEPLOY_RESULT=")))
    backup = target / ".release-backups" / outcome["backup"]
    assert (backup / "database.dump").read_text() == "disposable logical backup\n"

    (state / "events").write_text("")
    result = run(*TARGET, "--rollback", outcome["backup"])
    assert result.returncode == 0, result.stderr
    assert (target / "main.py").read_text() == 'RELEASE = "old"\n'
    assert not (target / "core/extra.py").exists()
    assert (state / "ledger").read_text().splitlines() == ledger
    assert (state / "events").read_text().splitlines() == ["ssh fixture", "inspect", "ledger 9", "pause"]
    assert_runtime(target, staging)


@pytest.mark.parametrize("flags, overrides, expected", [
    (("--schema", "002", "--all-code"), {}, "choose --target"),
    (("--target", "production", "--all-code"), {}, "choose --schema"),
    ((*TARGET, "data/forms/live.json"), {}, "not an immutable release path"),
    ((*TARGET, "core/untracked.py"), {}, "not a tracked release file"),
    ((*TARGET, "--all-code", "--apply-migrations"), {"FAKE_ENVIRONMENT": "development"}, "environment differs"),
    ((*TARGET, "--all-code", "--apply-migrations"), {"FAKE_DATABASE": "surveydb_dev"}, "requested database"),
    ((*TARGET, "--all-code", "--apply-migrations"), {"WRONG_WORKER_MOUNT": "1"}, "private-data bind mount"),
    ((*TARGET, "--all-code", "--apply-migrations"), {"FAKE_SQL_TARGET_REFUSED": "1"}, "target or ledger check failed"),
    ((*TARGET, "--all-code"), {}, "approve --apply-migrations first"),
])
def test_refuses_unsafe_targets_and_paths_before_pausing(release, flags, overrides, expected):
    run, target, staging, state, _ = release
    result = run(*flags, **overrides)
    assert result.returncode != 0 and expected in result.stderr, result.stdout + result.stderr
    assert 'never-print' not in result.stderr + result.stdout
    assert 'pause' not in (state / 'events').read_text()
    assert not (target / '.release-backups').exists()
    assert_runtime(target, staging)


def test_dry_run_and_remote_symlink_do_not_publish(release):
    run, target, staging, state, _ = release
    result = run(*TARGET, "--all-code", "--dry-run")
    assert result.returncode == 0 and "PLAN (local only)" in result.stdout, result.stderr
    assert not (state / "events").read_text()
    (target / "core/extra.py").symlink_to(target / "data/forms/live.json")
    result = run(*TARGET, "--all-code", "--apply-migrations")
    assert result.returncode != 0 and "unsafe code destination" in result.stderr
    assert (target / "core/extra.py").is_symlink()
    assert_runtime(target, staging)


def test_checksum_mismatch_does_not_pause_or_apply(release):
    run, _, _, state, _ = release
    (state / "ledger").write_text("001|wrong\n")
    result = run(*TARGET, "--all-code", "--apply-migrations")
    assert result.returncode != 0 and "unexpected migration version or checksum" in result.stderr
    assert "pause" not in (state / "events").read_text()


def test_failed_migration_does_not_publish_or_start(release):
    run, target, staging, state, ledger = release
    result = run(*TARGET, "--all-code", "--apply-migrations", FAIL_MIGRATION="002")
    assert result.returncode != 0 and "app and worker paused" in result.stderr
    assert (target / "main.py").read_text() == 'RELEASE = "old"\n'
    assert (state / "ledger").read_text().splitlines() == ledger[:1]
    events = (state / "events").read_text()
    assert "migrate 002" in events and "migrate 003" not in events and "start " not in events
    assert_runtime(target, staging)


@pytest.mark.parametrize("failure, app_started", [
    ({"HEARTBEAT": "null"}, False), ({"HEARTBEAT_BUILD": "old-build"}, False),
    ({"FAIL_READY": "1"}, True),
])
def test_failed_gate_rehearses_code_only_recovery(release, failure, app_started):
    run, target, staging, state, ledger = release
    result = run(*TARGET, "--all-code", "--apply-migrations", **failure)
    assert result.returncode != 0 and "app and worker paused" in result.stderr
    assert (target / "main.py").read_text() == 'RELEASE = "old"\n'
    assert not (target / "core/extra.py").exists()
    assert (state / "ledger").read_text().splitlines() == ledger
    events = (state / "events").read_text().splitlines()
    assert ("start app-id" in events) is app_started
    assert events[-1] == "pause"
    assert_runtime(target, staging)


def test_standalone_smoke_is_read_only_and_fails_closed_on_stale_heartbeat(release):
    run, _, _, state, ledger = release
    (state / "ledger").write_text("\n".join(ledger) + "\n")
    result = run(*TARGET, script="smoke_prod.sh")
    assert result.returncode == 0, result.stderr
    assert 'OUTBOX_METRICS={"failed": 2, "uncertain": 1' in result.stdout
    assert (state / "events").read_text().splitlines() == [
        "ssh fixture", "inspect", "ledger 9", "worker health", "GET /health", "GET /ready", "queue read"
    ]
    result = run(*TARGET, script="smoke_prod.sh", HEARTBEAT="121")
    assert result.returncode != 0 and "heartbeat check failed" in result.stderr
    assert (state / "ledger").read_text().splitlines() == ledger


def test_workflow_invokes_only_shared_manual_release_path():
    workflow = (REPO / ".github/workflows/pipeline-ci-cd.yml").read_text()
    assert "if: github.event_name == 'workflow_dispatch'" in workflow
    assert 'bash scripts/deploy_prod.sh "${args[@]}"' in workflow
    assert "rsync" not in workflow and "ssh-keyscan" not in workflow
