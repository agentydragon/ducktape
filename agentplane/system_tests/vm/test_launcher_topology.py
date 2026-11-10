"""The KubeVirt launcher must keep file-backed virtiofs daemons as native sidecars."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.vm import launcher_topology
from agentplane.system_tests.vm.launcher_topology import inspect_launcher_topology, is_vm_template


@dataclass(frozen=True)
class PodIdentity:
    name: str
    namespace: str
    uid: str


def _vmi(*, extra_filesystem_source: str | None = None) -> dict[str, Any]:
    volumes: list[dict[str, Any]] = [
        {"name": "state", "persistentVolumeClaim": {"claimName": "test-state"}},
        # A PVC-backed block disk is not a filesystem device and must not demand a virtiofs daemon.
        {"name": "workspace", "persistentVolumeClaim": {"claimName": "test-workspace"}},
    ]
    filesystems: list[dict[str, Any]] = [{"name": "state", "virtiofs": {}}]
    if extra_filesystem_source is not None:
        volumes.append({"name": "config", extra_filesystem_source: {"name": "test-config"}})
        filesystems.append({"name": "config", "virtiofs": {}})
    return {
        "metadata": {"name": "test-vmi", "namespace": "test-namespace", "uid": "vmi-uid"},
        "spec": {"domain": {"devices": {"filesystems": filesystems}}, "volumes": volumes},
        "status": {"phase": "Running"},
    }


def _daemon(name: str, *, restart_policy: str | None = None) -> dict[str, Any]:
    container: dict[str, Any] = {"name": name, "command": ["/usr/libexec/virtiofsd"]}
    if restart_policy is not None:
        container["restartPolicy"] = restart_policy
    return container


def _pod(
    *,
    containers: list[dict[str, Any]] | None = None,
    init_containers: list[dict[str, Any]] | None = None,
    init_statuses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": "virt-launcher-test-vmi-abcde",
            "namespace": "test-namespace",
            "uid": "pod-uid",
            "labels": {
                "kubevirt.io": "virt-launcher",
                "kubevirt.io/created-by": "vmi-uid",
                "vmi.kubevirt.io/id": "test-vmi",
            },
            "ownerReferences": [{"kind": "VirtualMachineInstance", "name": "test-vmi", "uid": "vmi-uid"}],
        },
        "spec": {"containers": containers or [], "initContainers": init_containers or []},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [],
            "initContainerStatuses": init_statuses or [],
        },
    }


def _running_status(name: str) -> dict[str, Any]:
    return {"name": name, "ready": True, "state": {"running": {"startedAt": "2026-11-06T00:00:00Z"}}}


def _issue_codes(report: dict[str, Any]) -> set[str]:
    issues = report["issues"]
    assert isinstance(issues, list)
    return {issue["code"] for issue in issues}


def test_policy_on_topology_passes_and_excludes_the_block_workspace_disk() -> None:
    pod = _pod(
        init_containers=[_daemon("virtiofs-state", restart_policy="Always")],
        init_statuses=[_running_status("virtiofs-state")],
    )

    report = inspect_launcher_topology(pod, _vmi())

    assert _issue_codes(report) == set()
    assert report["eligible_filesystems"] == [
        {
            "volume": "state",
            "source": "persistentVolumeClaim",
            "expected_daemon": "virtiofs-state",
            "ordinary_container_count": 0,
            "init_container_count": 1,
            "restart_policy": "Always",
            "status": "running",
            "ready": True,
        }
    ]


def test_policy_off_topology_reports_the_real_daemon_left_ordinary() -> None:
    report = inspect_launcher_topology(_pod(containers=[_daemon("virtiofs-state")]), _vmi())

    assert "virtiofs_daemon_is_ordinary_container" in _issue_codes(report)
    assert "virtiofs-state" in report["issues"][0]["detail"]


@pytest.mark.parametrize("source", ["persistentVolumeClaim", "secret", "configMap"])
def test_every_policy_file_backing_source_is_checked(source: str) -> None:
    pod = _pod(
        init_containers=[
            _daemon("virtiofs-state", restart_policy="Always"),
            _daemon("virtiofs-config", restart_policy="Always"),
        ],
        init_statuses=[_running_status("virtiofs-state"), _running_status("virtiofs-config")],
    )

    report = inspect_launcher_topology(pod, _vmi(extra_filesystem_source=source))

    assert _issue_codes(report) == set()
    assert {filesystem["source"] for filesystem in report["eligible_filesystems"]} == {"persistentVolumeClaim", source}


def test_container_path_virtiofs_daemon_remains_ordinary() -> None:
    vmi = _vmi()
    vmi["spec"]["domain"]["devices"]["filesystems"].append({"name": "container-path", "virtiofs": {}})
    vmi["spec"]["volumes"].append({"name": "container-path", "containerPath": {"path": "/config"}})
    pod = _pod(
        containers=[_daemon("virtiofs-container-path")],
        init_containers=[_daemon("virtiofs-state", restart_policy="Always")],
        init_statuses=[_running_status("virtiofs-state")],
    )

    report = inspect_launcher_topology(pod, vmi)

    assert _issue_codes(report) == set()
    assert [filesystem["volume"] for filesystem in report["eligible_filesystems"]] == ["state"]


def test_missing_file_backed_daemon_is_reported() -> None:
    report = inspect_launcher_topology(_pod(), _vmi())

    assert "virtiofs_daemon_missing" in _issue_codes(report)


def test_file_source_without_virtiofs_device_does_not_require_a_daemon() -> None:
    vmi = _vmi()
    vmi["spec"]["volumes"].append({"name": "config", "configMap": {"name": "test-config"}})
    vmi["spec"]["domain"]["devices"]["filesystems"].append({"name": "config"})
    pod = _pod(
        init_containers=[_daemon("virtiofs-state", restart_policy="Always")],
        init_statuses=[_running_status("virtiofs-state")],
    )

    report = inspect_launcher_topology(pod, vmi)

    assert _issue_codes(report) == set()
    assert [filesystem["volume"] for filesystem in report["eligible_filesystems"]] == ["state"]


def test_file_source_with_null_virtiofs_device_does_not_require_a_daemon() -> None:
    vmi = _vmi()
    vmi["spec"]["volumes"].append({"name": "config", "configMap": {"name": "test-config"}})
    vmi["spec"]["domain"]["devices"]["filesystems"].append({"name": "config", "virtiofs": None})
    pod = _pod(
        init_containers=[_daemon("virtiofs-state", restart_policy="Always")],
        init_statuses=[_running_status("virtiofs-state")],
    )

    report = inspect_launcher_topology(pod, vmi)

    assert _issue_codes(report) == set()
    assert [filesystem["volume"] for filesystem in report["eligible_filesystems"]] == ["state"]


def test_launcher_is_correlated_by_kubevirt_vmi_id_label() -> None:
    pod = _pod()
    pod["metadata"]["labels"]["vmi.kubevirt.io/id"] = "stale-vmi"

    report = inspect_launcher_topology(pod, _vmi())

    assert _issue_codes(report) == {"virtiofs_daemon_missing", "launcher_vmi_id_label_mismatch"}


def test_sidecar_restart_policy_and_running_status_are_required() -> None:
    report = inspect_launcher_topology(
        _pod(
            init_containers=[_daemon("virtiofs-state", restart_policy="OnFailure")],
            init_statuses=[{"name": "virtiofs-state", "state": {"terminated": {"exitCode": 1}}}],
        ),
        _vmi(),
    )

    assert _issue_codes(report) == {
        "virtiofs_daemon_restart_policy_mismatch",
        "virtiofs_daemon_not_running",
        "virtiofs_daemon_not_ready",
    }


def test_sidecar_must_still_run_the_virtiofsd_command() -> None:
    pod = _pod(
        init_containers=[{"name": "virtiofs-state", "command": ["/bin/false"], "restartPolicy": "Always"}],
        init_statuses=[_running_status("virtiofs-state")],
    )

    report = inspect_launcher_topology(pod, _vmi())

    assert _issue_codes(report) == {"virtiofs_daemon_command_mismatch"}


def test_vm_template_match_does_not_select_ordinary_pod_templates() -> None:
    assert is_vm_template("environment-vm")
    assert is_vm_template("environment-vm-codex-oom")
    assert not is_vm_template("environment-tools")


async def test_live_observer_waits_for_the_exact_pod_and_retains_sanitized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_topology, "undeclared_outputs_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher_topology, "TOPOLOGY_POLL_SECONDS", 0)

    class FakeCluster:
        namespace = "test-namespace"

        def __init__(self) -> None:
            self.pod_reads = 0

        async def kubectl(self, *arguments: str, **_kwargs: Any) -> str:
            if arguments[:2] == ("get", "pod"):
                self.pod_reads += 1
                if self.pod_reads == 1:
                    raise RuntimeError("Error from server (NotFound): pods not found")
                return json.dumps(
                    _pod(
                        init_containers=[_daemon("virtiofs-state", restart_policy="Always")],
                        init_statuses=[_running_status("virtiofs-state")],
                    )
                )
            if arguments[:2] == ("get", "virtualmachineinstance"):
                return json.dumps(_vmi())
            raise AssertionError(f"unexpected kubectl call: {arguments}")

    projected_pod = PodIdentity(name="virt-launcher-test-vmi-abcde", namespace="test-namespace", uid="pod-uid")
    cluster = FakeCluster()

    await launcher_topology.assert_admitted_launcher_topology(cluster, "test-sandbox", "sandbox-uid", projected_pod)

    report_path = tmp_path / "vm-artifacts/launcher-topology/pod-uid.json"
    report = json.loads(report_path.read_text())
    assert cluster.pod_reads == 2
    assert report["observation"]["state"] == "observed_ready_pair"
    assert report["pod"]["uid"] == "pod-uid"
    assert report["vmi"]["uid"] == "vmi-uid"


async def test_api_error_is_not_reported_as_a_topology_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_topology, "undeclared_outputs_dir", lambda: tmp_path)

    class ForbiddenCluster:
        namespace = "test-namespace"

        async def kubectl(self, *_arguments: str, **_kwargs: Any) -> str:
            raise RuntimeError("Error from server (Forbidden): pods is forbidden")

    projected_pod = PodIdentity(name="virt-launcher-test-vmi-abcde", namespace="test-namespace", uid="pod-uid")

    with pytest.raises(launcher_topology.LauncherTopologyObservationError):
        await launcher_topology.assert_admitted_launcher_topology(
            ForbiddenCluster(), "test-sandbox", "sandbox-uid", projected_pod
        )

    report_path = tmp_path / "vm-artifacts/launcher-topology/pod-uid.json"
    assert json.loads(report_path.read_text())["observation"]["state"] == "api_error"


async def test_missing_pod_after_deadline_is_not_reported_as_a_topology_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_topology, "undeclared_outputs_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher_topology, "TOPOLOGY_OBSERVATION_SECONDS", 0)

    class MissingPodCluster:
        namespace = "test-namespace"

        async def kubectl(self, *_arguments: str, **_kwargs: Any) -> str:
            raise RuntimeError("Error from server (NotFound): pods not found")

    projected_pod = PodIdentity(name="virt-launcher-test-vmi-abcde", namespace="test-namespace", uid="pod-uid")

    with pytest.raises(launcher_topology.LauncherTopologyObservationError):
        await launcher_topology.assert_admitted_launcher_topology(
            MissingPodCluster(), "test-sandbox", "sandbox-uid", projected_pod
        )

    report_path = tmp_path / "vm-artifacts/launcher-topology/pod-uid.json"
    report = json.loads(report_path.read_text())
    assert report["observation"]["state"] == "pod_not_found"
    assert "issues" not in report


async def test_pod_in_another_namespace_is_rejected_before_kubectl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_topology, "undeclared_outputs_dir", lambda: tmp_path)

    class NamespaceCluster:
        namespace = "test-namespace"

        async def kubectl(self, *_arguments: str, **_kwargs: Any) -> str:
            raise AssertionError("must not query outside the fixture namespace")

    projected_pod = PodIdentity(name="virt-launcher-test-vmi-abcde", namespace="other-namespace", uid="pod-uid")

    with pytest.raises(launcher_topology.LauncherTopologyObservationError):
        await launcher_topology.assert_admitted_launcher_topology(
            NamespaceCluster(), "test-sandbox", "sandbox-uid", projected_pod
        )

    report_path = tmp_path / "vm-artifacts/launcher-topology/pod-uid.json"
    assert json.loads(report_path.read_text())["observation"]["state"] == "projected_pod_namespace_mismatch"


if __name__ == "__main__":
    pytest_bazel.main()
