"""Stop only the v1 Firecracker baseline named by a completed experiment receipt."""

import json
import os
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.k3s import baseline
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def test_cleanup_prepared_firecracker_baseline_v1() -> None:
    receipt_json = os.environ.get("AGENTPLANE_EXPECTED_PERSISTENCE_RECEIPT")
    if not receipt_json:
        pytest.fail("cleanup requires the exact persistence receipt from the prepared-baseline action")
    receipt = json.loads(receipt_json)
    expected_fingerprint = receipt.get("fingerprint")
    expected_identity = receipt.get("k3s")
    if not isinstance(expected_fingerprint, str) or not isinstance(expected_identity, dict):
        pytest.fail("receipt must contain string fingerprint and K3s identity fields")
    if receipt.get("outcome") != "warm" or receipt.get("cleanup") != "complete":
        pytest.fail("cleanup requires the completed warm-reuse receipt, not a cold or incomplete action")

    marker_path = baseline._BASELINE_MARKER
    if not marker_path.is_file():
        pytest.fail(f"the v1 baseline marker is absent on this recycled runner: {marker_path}")
    marker = json.loads(marker_path.read_text())
    if marker.get("data_dir") is not None or baseline._marker_data_dir(marker) != Path("/var/lib/k3s-env"):
        pytest.fail("refusing to use the v1 cleanup target for a non-v1 K3s data directory")
    if marker.get("state") != "clean":
        pytest.fail(f"expected a clean retained baseline, found {marker.get('state')!r}")
    if marker.get("fingerprint") != expected_fingerprint or marker.get("identity") != expected_identity:
        pytest.fail("local v1 marker does not match the completed action receipt; refusing cleanup")

    expected_pid = expected_identity.get("pid")
    expected_boot_id = expected_identity.get("boot_id")
    expected_start_ticks = expected_identity.get("process_start_ticks")
    if (
        not isinstance(expected_pid, int)
        or isinstance(expected_pid, bool)
        or not isinstance(expected_boot_id, str)
        or not isinstance(expected_start_ticks, str)
    ):
        pytest.fail("receipt must identify the K3s process with PID, boot ID, and start ticks")
    current_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if current_boot_id != expected_boot_id:
        pytest.fail("receipt K3s process belongs to a different boot; refusing shared-store cleanup")
    if baseline._baseline_process_start(expected_pid) != expected_start_ticks:
        pytest.fail("receipt K3s process is not the live PID incarnation; refusing shared-store cleanup")

    retained_data_dir = Path("/var/lib/k3s-env")
    legacy = baseline.K3s(baseline._BASELINE_ROOT, baseline._BASELINE_ROOT / "k3s", data_dir=retained_data_dir)
    if baseline._baseline_identity(legacy, expected_pid) != expected_identity:
        pytest.fail("live retained K3s identity does not match the full completed receipt")
    argv = [os.fsdecode(part) for part in Path(f"/proc/{expected_pid}/cmdline").read_bytes().split(b"\0") if part]
    if not argv or argv[0] != str(legacy.binary) or len(argv) < 2 or argv[1] != "server":
        pytest.fail("receipt process command line does not name the retained K3s server binary")
    data_dir_flags = [index for index, arg in enumerate(argv) if arg == "--data-dir"]
    if len(data_dir_flags) != 1 or argv[data_dir_flags[0] + 1 : data_dir_flags[0] + 2] != [str(retained_data_dir)]:
        pytest.fail("receipt process command line does not use the exact v1 shared K3s data directory")

    current_argv = [
        os.fsdecode(part) for part in Path(f"/proc/{expected_pid}/cmdline").read_bytes().split(b"\0") if part
    ]
    if (
        Path("/proc/sys/kernel/random/boot_id").read_text().strip() != expected_boot_id
        or baseline._baseline_process_start(expected_pid) != expected_start_ticks
        or current_argv != argv
        or json.loads(marker_path.read_text()) != marker
    ):
        pytest.fail("retained K3s owner changed during validation; refusing shared-store cleanup")
    baseline.stop_previous()
    if marker_path.exists():
        pytest.fail("owned baseline marker remains after stop_previous")
    process_start_after_stop = baseline._baseline_process_start(expected_pid)
    if process_start_after_stop == expected_start_ticks:
        pytest.fail("the receipt's K3s process is still alive after stop_previous")

    (undeclared_outputs_dir() / "prepared-firecracker-baseline-v1-cleanup.json").write_text(
        json.dumps(
            {
                "data_dir": str(retained_data_dir),
                "fingerprint": expected_fingerprint,
                "identity": expected_identity,
                "marker_removed": True,
                "owner_verified": True,
                "owned_process_stopped": True,
                "process_start_ticks_after_stop": process_start_after_stop,
                "verified_current_boot_id": current_boot_id,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    pytest_bazel.main()
