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


def test_cgroup_procs_check_uses_current_shell_and_rejects_extra_pids(tmp_path: Path) -> None:
    helper = _runfile("DELEGATED_CGROUP_STRESS_HELPERS_RLOCATION")
    proc_file = tmp_path / "cgroup.procs"
    shell = """
. "$1"
procs=$2
printf '%s\\n' "$$" >"$procs"
cgroup_has_only_pid "$procs" "$$"
printf '%s\\n' 987654 >>"$procs"
if cgroup_has_only_pid "$procs" "$$"; then
    exit 1
fi
printf '%s\\n' "$$" >"$procs"
if cgroup_has_only_pid "$procs" 987654; then
    exit 1
fi
: >"$procs"
if cgroup_has_only_pid "$procs" "$$"; then
    exit 1
fi
"""
    subprocess.run([str(_busybox(tmp_path)), "sh", "-c", shell, "sh", str(helper), str(proc_file)], check=True)


def test_trace_filter_empty_sentinels_do_not_accept_real_filters(tmp_path: Path) -> None:
    helper = _runfile("DELEGATED_CGROUP_STRESS_HELPERS_RLOCATION")
    busybox = _busybox(tmp_path)
    trace_filter = tmp_path / "filter"
    shell = r"""
busybox=$1
. "$2"
file=$3
for value in '' '#### all functions enabled ####' '#### no functions disabled ####' 'no pid'; do
    printf '%s\n' "$value" >"$file"
    filter_is_empty "$file" || exit 1
done
printf '%s\n' '#### no functions disabled ####' 'do_not_trace_me' >"$file"
if filter_is_empty "$file"; then exit 1; fi
printf '%s\n' 'do_not_trace_me' >"$file"
if filter_is_empty "$file"; then exit 1; fi
"""
    subprocess.run([str(busybox), "sh", "-c", shell, "sh", str(busybox), str(helper), str(trace_filter)], check=True)


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
