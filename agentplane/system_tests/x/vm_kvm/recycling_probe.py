"""Opt-in receipts and owned-runner disposal for the Docker recycling experiment."""

import contextlib
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
    _RECEIPT.write_text(json.dumps(receipt))
    (undeclared_outputs_dir() / "recycled-runner.json").write_text(json.dumps(receipt))
    if receipt["discard_requested"]:
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(baseline.discard_executor_runner)
            _stop_baseline()
