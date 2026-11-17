"""Opt-in receipts and owned-runner disposal for the Docker recycling experiment."""

import contextlib
import errno
import json
import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from python.runfiles import Runfiles

from agentplane.system_tests.k3s import baseline
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

_RECEIPT = Path("/var/lib/k3s-recycling-probe.json")
_DATA = Path("/var/lib/k3s-recycled")
_BACKING = Path("/var/lib/k3s-recycled.ext4")
_CGROUP = Path("/sys/fs/cgroup")


def _memory() -> dict[str, object]:
    return {
        "current": int((_CGROUP / "memory.current").read_text()),
        "stat": dict(line.split() for line in (_CGROUP / "memory.stat").read_text().splitlines()),
        "events": dict(line.split() for line in (_CGROUP / "memory.events").read_text().splitlines()),
    }


def _reclaim_file_cache() -> int | None:
    # K3s moves the container's initial processes into /init before enabling child
    # controllers. Both paths are relative to this action's private cgroup namespace.
    if not Path("/.dockerenv").exists() or Path("/proc/self/cgroup").read_text().strip() not in {"0::/", "0::/init"}:
        raise RuntimeError("cache reclamation requires this Docker runner's private cgroup")
    # The pinned executor counts v2 file cache against its 2 GB retention cap. Reclaim only
    # this cgroup, leaving the kernel's anonymous-memory policy and the pool cap unchanged.
    amount = max(0, int((_CGROUP / "memory.current").read_text()) - 1_500_000_000)
    if amount:
        try:
            (_CGROUP / "memory.reclaim").write_text(f"{amount} swappiness=0")
        except OSError as error:
            if error.errno != errno.EAGAIN:
                raise
            # EAGAIN means the kernel could not reclaim the full requested amount.
            return error.errno
    return None


def _prepare_data_disk() -> None:
    if os.environ.get("AGENTPLANE_K3S_DATA_DIR") != str(_DATA):
        raise RuntimeError("recycling probe requires its private K3s directory")
    if _DATA.is_mount():
        return
    if _BACKING.exists():
        raise RuntimeError("retained data disk lost its mount; refusing to reuse unverified state")
    archive = Runfiles.CreateOrRaise().Rlocation(os.environ["K3S_RECYCLING_DISK_RLOCATION"])
    assert archive is not None
    subprocess.run(["tar", "-xf", archive, "-C", _BACKING.parent], check=True)
    _DATA.mkdir(exist_ok=True)
    subprocess.run(["mount", "-t", "ext4", "-o", "loop", _BACKING, _DATA], check=True)


def _stop_baseline() -> None:
    baseline.stop_previous()
    if _DATA.is_mount():
        # mount -o loop uses AUTOCLEAR: the mapping disappears when its last mount closes.
        subprocess.run(["umount", _DATA], check=True)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--discard-recycled-runner", action="store_true", help="Stop the owned baseline and discard this runner"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    previous = json.loads(_RECEIPT.read_text()) if _RECEIPT.exists() else None
    receipt = {
        "runner_instance": previous["runner_instance"] if previous else str(uuid4()),
        "action_instance": str(uuid4()),
        "previous_action": previous["action_instance"] if previous else None,
        "previous_exit_status": previous.get("exit_status") if previous else None,
        "container_hostname": socket.gethostname(),
        "pid_namespace": str(Path("/proc/self/ns/pid").readlink()),
        "mount_namespace": str(Path("/proc/self/ns/mnt").readlink()),
        "cgroup_namespace": str(Path("/proc/self/ns/cgroup").readlink()),
        "cgroup_membership_at_start": Path("/proc/self/cgroup").read_text().strip(),
        "data_directory": os.environ.get("AGENTPLANE_K3S_DATA_DIR"),
    }
    _RECEIPT.write_text(json.dumps(receipt))
    (undeclared_outputs_dir() / "recycled-runner.json").write_text(json.dumps(receipt))
    try:
        _prepare_data_disk()
        receipt["data_mount"] = next(
            line for line in Path("/proc/self/mountinfo").read_text().splitlines() if line.split()[4] == str(_DATA)
        )
        _RECEIPT.write_text(json.dumps(receipt))
        (undeclared_outputs_dir() / "recycled-runner.json").write_text(json.dumps(receipt))
    except BaseException:
        baseline.discard_executor_runner()
        raise


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    receipt = json.loads(_RECEIPT.read_text())
    receipt["exit_status"] = int(exitstatus)
    receipt["discard_requested"] = bool(session.config.getoption("--discard-recycled-runner") or exitstatus)
    try:
        receipt["memory_before_reclaim"] = _memory()
        if not receipt["discard_requested"]:
            receipt["reclaim_errno"] = _reclaim_file_cache()
        receipt["memory_after_reclaim"] = _memory()
    except BaseException:
        receipt["discard_requested"] = True
        baseline.discard_executor_runner()
        raise
    _RECEIPT.write_text(json.dumps(receipt))
    (undeclared_outputs_dir() / "recycled-runner.json").write_text(json.dumps(receipt))
    if receipt["discard_requested"]:
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(baseline.discard_executor_runner)
            _stop_baseline()
