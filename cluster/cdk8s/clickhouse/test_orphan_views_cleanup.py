"""The destructive cleanup must preflight every target before dropping anything."""

import os
import subprocess
from pathlib import Path

import pytest_bazel

from util.bazel.runfiles import get_required_path, own_repo_rlocation

_SCRIPT = get_required_path(own_repo_rlocation("cluster/cdk8s/clickhouse/orphan-views-cleanup.sh"))


def _run(tmp_path: Path, *, fail_host: str = "") -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake = tmp_path / "clickhouse-client"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
        'case "$*" in *"$FAIL_HOST"*) [ -z "$FAIL_HOST" ] || exit 1;; esac\n'
    )
    fake.chmod(0o755)
    log = tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CALL_LOG": str(log),
        "FAIL_HOST": fail_host,
        "CLICKHOUSE_REPLICA_HOSTS": "replica-0 replica-1",
    }
    result = subprocess.run(["/bin/sh", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False)
    return result, log.read_text().splitlines()


def test_checks_all_hosts_before_deleting(tmp_path: Path) -> None:
    result, calls = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 12
    assert "--host replica-0" in calls[0]
    assert "--host replica-1" in calls[1]
    assert all("DROP VIEW" not in call for call in calls[:3])
    assert sum("DROP VIEW" in call for call in calls) == 4


def test_preflight_failure_does_not_delete_any_view(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, fail_host="replica-1")
    assert result.returncode != 0
    assert len(calls) == 2
    assert all("DROP VIEW" not in call for call in calls)


if __name__ == "__main__":
    pytest_bazel.main()
