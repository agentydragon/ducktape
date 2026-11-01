"""Bounded RBE spike: can one test action reconnect to a live k3s from another?"""

import hashlib
import json
import os
import shutil
from pathlib import Path

from agentplane.system_tests.k3s.stack import K3s
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

ROOT = Path("/var/lib/k3s-persistence-spike-v3")
MARKER = ROOT / "server.json"
FINGERPRINT_VERSION = "k3s-persistence-spike-v3-de5f671002"


def _start_ticks(pid: int) -> str:
    # The start time distinguishes a retained process from PID reuse within one VM boot.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def _identity(k3s: K3s, pid: int) -> dict[str, str | int]:
    internal_ip = k3s.kubectl(
        "get", "node", "env", "-o", "jsonpath={.status.addresses[?(@.type=='InternalIP')].address}"
    )
    assert internal_ip == k3s.node_ip, (internal_ip, k3s.node_ip)
    return {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": pid,
        "process_start_ticks": _start_ticks(pid),
        "namespace_uid": k3s.kubectl("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"),
        "node_uid": k3s.kubectl("get", "node", "env", "-o", "jsonpath={.metadata.uid}"),
        "node_internal_ip": internal_ip,
    }


def test_k3s_survives_action() -> None:
    ROOT.mkdir(exist_ok=True)
    source = get_required_path(os.environ["K3S_BINARY_RLOCATION"])
    binary = ROOT / "k3s"
    binary_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    fingerprint = f"{FINGERPRINT_VERSION}:{binary_digest}"
    previous = json.loads(MARKER.read_text()) if MARKER.exists() else None
    if previous is not None:
        assert previous["fingerprint"] == fingerprint, "spike key reused with incompatible k3s inputs"
        k3s = K3s(ROOT, binary)
        try:
            current = _identity(k3s, previous["identity"]["pid"])
        except (OSError, RuntimeError) as exc:
            try:
                old_start_ticks = _start_ticks(previous["identity"]["pid"])
            except OSError:
                old_start_ticks = None
            if old_start_ticks == previous["identity"]["process_start_ticks"]:
                raise RuntimeError(
                    "retained k3s process is alive but API is unhealthy; refusing a second server"
                ) from exc
            print(json.dumps({"outcome": "stale", "reason": str(exc), "previous": previous}))
        else:
            assert current == previous["identity"], (current, previous)
            warm_result = {"outcome": "warm", "identity": current, "fingerprint": fingerprint}
            (undeclared_outputs_dir() / "persistence-spike.json").write_text(json.dumps(warm_result))
            print(json.dumps(warm_result))
            return

    shutil.copy2(source, binary)
    k3s = K3s(ROOT, binary)
    # K3s.start normally writes under Bazel's per-action output directory. The server may outlive
    # that directory, so its log and kubeconfig must live on the retained VM filesystem.
    previous_outputs = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    os.environ["TEST_UNDECLARED_OUTPUTS_DIR"] = str(ROOT)
    try:
        process = k3s.start(layers_in_memory=True, detached=True)
    finally:
        if previous_outputs is None:
            os.environ.pop("TEST_UNDECLARED_OUTPUTS_DIR", None)
        else:
            os.environ["TEST_UNDECLARED_OUTPUTS_DIR"] = previous_outputs
    identity = _identity(k3s, process.pid)
    MARKER.write_text(json.dumps({"fingerprint": fingerprint, "identity": identity}))
    cold_result: dict[str, object] = {
        "outcome": "cold",
        "prior_marker_present": previous is not None,
        "identity": identity,
        "fingerprint": fingerprint,
    }
    (undeclared_outputs_dir() / "persistence-spike.json").write_text(json.dumps(cold_result))
    print(json.dumps(cold_result))


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
