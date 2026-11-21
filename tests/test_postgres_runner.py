# -*- coding: utf-8 -*-
"""Ownership/isolation checks for the disposable PostgreSQL runner.

These use a fake Docker CLI and never touch a real daemon.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_postgres.sh"


def _fake_docker(tmp_path: Path) -> Path:
    log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1 $2" in
  "context show") echo "${FAKE_DOCKER_CONTEXT:-default}" ;;
  "context inspect") echo "${FAKE_DOCKER_ENDPOINT:-unix:///var/run/docker.sock}" ;;
  "ps -a") [ -n "${FAKE_DOCKER_EXISTING:-}" ] && echo "${FAKE_DOCKER_EXISTING}" || true ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker


def _run_dry(tmp_path: Path, **extra):
    docker = _fake_docker(tmp_path)
    log = tmp_path / "docker.log"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "PYTHON": sys.executable,
        "FAKE_DOCKER_LOG": str(log),
        "UPGRADE_TEST_POSTGRES_DRY_RUN": "1",
        **extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ), log


def test_dry_run_does_not_remove_or_start_containers(tmp_path):
    result, log = _run_dry(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "DRY_RUN" in result.stdout
    calls = log.read_text(encoding="utf-8").splitlines()
    assert not any(" run " in f" {call} " for call in calls), calls
    assert not any(" rm " in f" {call} " for call in calls), calls


def test_existing_caller_supplied_container_is_not_reused(tmp_path):
    result, log = _run_dry(
        tmp_path,
        UPGRADE_TEST_POSTGRES_NAME="survey-pipeline-wave0-pg",
        FAKE_DOCKER_EXISTING="existing-container-id",
    )
    assert result.returncode != 0
    assert "refusing to reuse existing container" in result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert not any(" rm " in f" {call} " for call in calls), calls
    assert not any(" run " in f" {call} " for call in calls), calls


def test_remote_docker_endpoint_requires_explicit_opt_in(tmp_path):
    result, log = _run_dry(
        tmp_path,
        FAKE_DOCKER_ENDPOINT="tcp://example.invalid:2375",
    )
    assert result.returncode != 0
    assert "refusing remote Docker endpoint" in result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert not any(" rm " in f" {call} " for call in calls), calls
    assert not any(" run " in f" {call} " for call in calls), calls
