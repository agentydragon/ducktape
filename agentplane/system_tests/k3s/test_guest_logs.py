"""Guest log retention across launcher removal and container log replacement."""

from pathlib import Path

import pytest
import pytest_bazel
from tenacity import Retrying, stop_after_delay, wait_fixed

from agentplane.system_tests.k3s.guest_logs import SHUTDOWN_COMPLETE, GuestShutdownError, capture

MARKER = "2026-10-27T21:41:13.112914838Z stdout F " + SHUTDOWN_COMPLETE.decode() + "\n"


def test_retains_unlinked_logs_and_new_incarnations(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    other = logs / "other_virt-launcher-test_uid-two" / "guest-console-log" / "0.log"
    other.parent.mkdir(parents=True)
    other.write_text("OUTSIDE_NAMESPACE\n")

    def wait_for(text: str) -> None:
        for attempt in Retrying(stop=stop_after_delay(5), wait=wait_fixed(0.01), reraise=True):
            with attempt:
                assert text in "".join(path.read_text() for path in outputs.rglob("*.log"))

    with (
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
        ),
        source.open("w") as original,
    ):
        original.write("BOOT\n")
        original.flush()
        wait_for("BOOT")
        source.unlink()
        original.write("AFTER_UNLINK\n")
        original.flush()
        wait_for("AFTER_UNLINK")
        source.write_text("NEW_CONTAINER\n")
        wait_for("NEW_CONTAINER")
        replacement = logs / "systest_virt-launcher-test_uid-three" / "guest-console-log" / "0.log"
        replacement.parent.mkdir(parents=True)
        replacement.write_text("NEW_LAUNCHER\n" + MARKER)
        wait_for("NEW_LAUNCHER")
        # Do not wait for another poll: capture's final drain must collect this from the unlinked fd.
        original.write(MARKER)
        original.flush()

    assert sorted(path.read_text() for path in outputs.rglob("*.log")) == [
        "BOOT\nAFTER_UNLINK\n" + MARKER,
        "NEW_CONTAINER\n",
        "NEW_LAUNCHER\n" + MARKER,
    ]


def test_requires_shutdown_for_each_launcher(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    traced_echo = MARKER.replace("stdout F ", "stdout F + echo ")
    for uid, content in [("one", MARKER), ("two", traced_echo)]:
        source = logs / f"systest_virt-launcher-test_uid-{uid}" / "guest-console-log" / "0.log"
        source.parent.mkdir(parents=True)
        source.write_text(content)

    with (
        pytest.raises(GuestShutdownError, match="systest_virt-launcher-test_uid-two"),
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
        ),
    ):
        pass


def test_retains_failure_evidence_without_masking_exception(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text("FAILED_GUEST\n")
    failure = ValueError("original scenario failure")
    with (
        pytest.raises(ValueError, match="original scenario failure") as caught,
        capture(
            log_root=logs,
            namespace="systest",
            kubevirt_namespace="kubevirt",
            destination=outputs,
            validate_shutdown=lambda: True,
        ),
    ):
        raise failure
    assert caught.value is failure
    assert [path.read_text() for path in outputs.rglob("*.log")] == ["FAILED_GUEST\n"]


def test_session_failures_disable_validation_at_teardown(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    source = logs / "systest_virt-launcher-test_uid-one" / "guest-console-log" / "0.log"
    source.parent.mkdir(parents=True)
    source.write_text("FAILED_GUEST\n")
    failures = 0
    with capture(
        log_root=logs,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
        validate_shutdown=lambda: failures == 0,
    ):
        # pytest reports failed cases without throwing their exception into the session fixture.
        failures += 1
    assert [path.read_text() for path in outputs.rglob("*.log")] == ["FAILED_GUEST\n"]


def test_retains_only_scoped_console_compute_and_handler_logs(tmp_path: Path) -> None:
    logs, outputs = tmp_path / "pods", tmp_path / "artifacts"
    contents = {
        "systest_virt-launcher-test_uid-one/guest-console-log/0.log": MARKER,
        "systest_virt-launcher-test_uid-one/compute/0.log": "COMPUTE\n",
        "kubevirt_virt-handler-test_uid-two/virt-handler/0.log": "HANDLER\n",
        "other_virt-launcher-test_uid-three/compute/0.log": "FOREIGN_LAUNCHER\n",
        "other_virt-handler-test_uid-four/virt-handler/0.log": "FOREIGN_HANDLER\n",
        "systest_virt-launcher-test_uid-one/egress/0.log": "SIDECAR\n",
        "kubevirt_virt-controller-test_uid-five/virt-controller/0.log": "CONTROLLER\n",
    }
    for relative, content in contents.items():
        path = logs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    with capture(
        log_root=logs,
        namespace="systest",
        kubevirt_namespace="kubevirt",
        destination=outputs,
        validate_shutdown=lambda: True,
    ):
        pass
    assert sorted(path.read_text() for path in outputs.rglob("*.log")) == sorted([MARKER, "COMPUTE\n", "HANDLER\n"])
    assert {path.parts[0] for path in (path.relative_to(outputs) for path in outputs.rglob("*.log"))} == {
        "consoles",
        "launchers",
        "handlers",
    }


if __name__ == "__main__":
    pytest_bazel.main()
