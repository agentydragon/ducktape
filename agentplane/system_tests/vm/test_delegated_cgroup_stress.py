"""Check the shipped delegated-cgroup probe syntax and systemd contract."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles


def _runfile(environment_name: str) -> Path:
    path = Runfiles.CreateOrRaise().Rlocation(os.environ[environment_name])
    assert path is not None
    return Path(path)


def _busybox(tmp_path: Path) -> Path:
    archive_path = _runfile("DELEGATED_CGROUP_STRESS_BUSYBOX_RLOCATION")
    binary = tmp_path / "busybox"
    with tarfile.open(archive_path) as archive:
        member = next(member for member in archive if member.name.endswith("bin/busybox"))
        content = archive.extractfile(member)
        assert content is not None
        binary.write_bytes(content.read())
    binary.chmod(0o755)
    return binary


def test_probe_has_valid_guest_busybox_shell_syntax(tmp_path: Path) -> None:
    script = _runfile("DELEGATED_CGROUP_STRESS_SCRIPT_RLOCATION")
    subprocess.run([str(_busybox(tmp_path)), "sh", "-n", str(script)], check=True)


def test_unit_uses_an_opt_in_systemd_delegation_boundary() -> None:
    service = _runfile("DELEGATED_CGROUP_STRESS_SERVICE_RLOCATION").read_text().splitlines()
    timer = _runfile("DELEGATED_CGROUP_STRESS_TIMER_RLOCATION").read_text().splitlines()
    assert "ConditionKernelCommandLine=agentplane.delegated-cgroup-stress=1" in service
    assert "Delegate=io" in service
    assert "DelegateSubgroup=manager" in service
    assert "TimeoutStartSec=90s" in service
    assert "ConditionKernelCommandLine=agentplane.delegated-cgroup-stress=1" in timer
    assert "Unit=agentplane-delegated-cgroup-stress.service" in timer


if __name__ == "__main__":
    pytest_bazel.main()
