"""Exercise ownership and invalidation paths for a retained bare k3s process."""

import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.k3s import stack
from agentplane.system_tests.k3s.stack import K3s
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir


def test_persistence_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Cover clean attach, dirty/fingerprint rebuilds, dead servers, and PID reuse safely."""
    binary = get_required_path(os.environ["K3S_BINARY_RLOCATION"])
    base_fingerprint = (
        "persistence-lifecycle-v1:" + uuid.uuid4().hex + ":" + hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    fingerprint = base_fingerprint + ":post-start-fault"
    changed_fingerprint = fingerprint + ":changed"
    fingerprints = {fingerprint, changed_fingerprint}
    events: list[dict[str, Any]] = []
    k3s: K3s | None = None
    marker: dict[str, Any] | None = None
    unrelated: subprocess.Popen[bytes] | None = None
    failed_start_k3s: K3s | None = None
    failed_start_identity: dict[str, str | int] | None = None
    cleanup_succeeded = False

    def cleanup_owned() -> None:
        nonlocal cleanup_succeeded
        if k3s is not None and marker is not None:
            # Recover the real ownership record if the reused-PID probe left a forged marker.
            cleanup_marker = {**marker, "state": "dirty"}
            stack._baseline_write_marker(cleanup_marker)
            stack._baseline_stop_owned(k3s, cleanup_marker)

        # If the post-start guard itself regresses, stop only the exact process it launched.
        if failed_start_k3s is not None and failed_start_identity is not None:
            pid = int(failed_start_identity["pid"])
            if stack._baseline_process_start(pid) == failed_start_identity["process_start_ticks"]:
                stack._baseline_stop_owned(failed_start_k3s, {"identity": failed_start_identity})

        if stack._BASELINE_MARKER.exists():
            persisted = json.loads(stack._BASELINE_MARKER.read_text())
            if persisted.get("fingerprint") in fingerprints:
                stack._BASELINE_MARKER.unlink()
        stack._BASELINE_MARKER.with_suffix(".tmp").unlink(missing_ok=True)
        cleanup_succeeded = True

    def write_evidence() -> None:
        undeclared_outputs_dir().joinpath("persistence-lifecycle.json").write_text(
            json.dumps({"steps": events, "cleanup": "complete" if cleanup_succeeded else "incomplete"})
        )

    with contextlib.ExitStack() as cleanup:
        # ExitStack runs these in reverse registration order: stop owned processes first,
        # then write the artifact even if an assertion or teardown fails.
        cleanup.callback(write_evidence)
        cleanup.callback(cleanup_owned)
        # Force the cold path, then fail after K3s.start has returned. The startup guard must
        # stop that owned process and leave no marker that could be mistaken for a clean seed.
        failed_start_pid: int | None = None

        def fail_identity_after_start(started_k3s: K3s, pid: int) -> None:
            nonlocal failed_start_k3s, failed_start_identity, failed_start_pid
            failed_start_pid = pid
            failed_start_k3s = started_k3s
            start_ticks = stack._baseline_process_start(pid)
            assert start_ticks is not None
            failed_start_identity = {
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "pid": pid,
                "process_start_ticks": start_ticks,
            }
            raise RuntimeError("injected identity failure after k3s start")

        with monkeypatch.context() as patch:
            patch.setattr(stack, "_baseline_identity", fail_identity_after_start)
            with pytest.raises(RuntimeError, match="injected identity failure"):
                stack._baseline_open(binary, fingerprint, layers_in_memory=True)
        assert failed_start_pid is not None
        assert stack._baseline_process_start(failed_start_pid) is None
        assert not stack._BASELINE_MARKER.exists()
        events.append({"step": "post_start_failure", "process_stopped": True, "marker_absent": True})

        # The failed attempt cannot be reused. Its next open must be a complete cold start.
        k3s, marker, recovered_warm = stack._baseline_open(binary, fingerprint, layers_in_memory=True)
        assert not recovered_warm
        assert marker["state"] == "dirty"
        events.append({"step": "post_start_failure_recovery", "outcome": "cold"})

        first_identity = marker["identity"].copy()
        events.append({"step": "initial_open", "outcome": "cold"})

        # A clean marker is the postcondition of a successful suite teardown. Reopen it and
        # require the same live process and Kubernetes identities before accepting warm reuse.
        marker["state"] = "clean"
        stack._baseline_write_marker(marker)
        attached_k3s, attached_marker, attached_warm = stack._baseline_open(binary, fingerprint, layers_in_memory=True)
        assert attached_warm
        assert attached_marker["identity"] == first_identity
        k3s, marker = attached_k3s, attached_marker
        events.append({"step": "clean_attach", "identity": marker["identity"]})

        # Dirty state with a real leaked API object must rebuild from an empty cluster.
        leaked_namespace = "persist-dirty-" + uuid.uuid4().hex[:12]
        k3s.kubectl("create", "namespace", leaked_namespace)
        assert k3s.kubectl("get", "namespace", leaked_namespace, "-o", "name").strip()
        k3s, marker, dirty_warm = stack._baseline_open(binary, fingerprint, layers_in_memory=True)
        assert not dirty_warm
        assert marker["identity"]["namespace_uid"] != first_identity["namespace_uid"]
        assert not k3s.kubectl("get", "namespace", leaked_namespace, "--ignore-not-found=true", "-o", "name").strip()
        events.append({"step": "dirty_rebuild", "leaked_namespace_removed": True})

        # A changed baseline fingerprint cannot attach to the clean old cluster.
        prior_identity = marker["identity"].copy()
        marker["state"] = "clean"
        stack._baseline_write_marker(marker)
        k3s, marker, changed_warm = stack._baseline_open(binary, changed_fingerprint, layers_in_memory=True)
        assert not changed_warm
        assert marker["fingerprint"] == changed_fingerprint
        assert marker["identity"]["namespace_uid"] != prior_identity["namespace_uid"]
        events.append({"step": "changed_fingerprint", "outcome": "cold"})

        # A clean marker whose server has exited must rebuild, without attaching to stale API data.
        dead_pid = int(marker["identity"]["pid"])
        dead_identity = marker["identity"].copy()
        marker["state"] = "clean"
        stack._baseline_write_marker(marker)
        os.kill(dead_pid, signal.SIGTERM)
        for attempt in Retrying(
            stop=stop_after_delay(stack.STOP_TIMEOUT_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(RuntimeError),
            reraise=True,
        ):
            with attempt:
                if stack._baseline_process_start(dead_pid) is not None:
                    raise RuntimeError(f"k3s process {dead_pid} is still alive")
        k3s, marker, dead_warm = stack._baseline_open(binary, changed_fingerprint, layers_in_memory=True)
        assert not dead_warm
        assert marker["identity"]["namespace_uid"] != dead_identity["namespace_uid"]
        events.append({"step": "dead_process", "outcome": "cold"})

        # A live unrelated process at the recorded PID with a different start time is never signaled.
        unrelated = cleanup.enter_context(
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], start_new_session=True)
        )
        cleanup.callback(_stop_unrelated, unrelated)
        unrelated_ticks = stack._baseline_process_start(unrelated.pid)
        assert unrelated_ticks is not None
        real_marker = marker.copy()
        real_marker["identity"] = marker["identity"].copy()
        forged_marker = json.loads(json.dumps(marker))
        forged_marker["state"] = "clean"
        forged_marker["identity"]["pid"] = unrelated.pid
        forged_marker["identity"]["process_start_ticks"] = "0"
        stack._baseline_write_marker(forged_marker)
        with pytest.raises(RuntimeError, match="PID was reused"):
            stack._baseline_open(binary, changed_fingerprint, layers_in_memory=True)
        with pytest.raises(RuntimeError, match="PID was reused"):
            stack._baseline_stop_previous()
        assert unrelated.poll() is None
        assert stack._baseline_process_start(unrelated.pid) == unrelated_ticks
        assert stack._baseline_identity(k3s, int(real_marker["identity"]["pid"])) == real_marker["identity"]
        marker = real_marker
        stack._baseline_write_marker(marker)
        events.append({"step": "reused_pid", "unrelated_process_alive": True})

        # Removing the executor permission can still restore a formerly retained snapshot.
        # Stop its recorded owner before a normal start resets shared kubelet/containerd state.
        retained_pid = int(marker["identity"]["pid"])
        with monkeypatch.context() as patch:
            patch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE")
            stack._baseline_stop_previous()
            assert stack._baseline_process_start(retained_pid) is None
            assert not stack._BASELINE_MARKER.exists()
            k3s, marker = None, None
            ordinary = K3s(tmp_path, binary)
            with contextlib.ExitStack() as ordinary_cleanup:
                process = ordinary.start(layers_in_memory=True)
                ordinary_cleanup.callback(ordinary.stop, process)
                assert process.poll() is None
                assert not stack._BASELINE_MARKER.exists()
            assert process.poll() is not None
            assert not stack._BASELINE_MARKER.exists()
        events.append({"step": "retention_disabled", "previous_owner_stopped": True, "ordinary_server_stopped": True})


def _stop_unrelated(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
