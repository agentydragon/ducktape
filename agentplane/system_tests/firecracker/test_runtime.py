"""Firecracker waits for a KVM resource without requiring VM admission policies."""

import json
from pathlib import Path

import pytest

from agentplane.system_tests.firecracker import runtime
from agentplane.system_tests.k3s import baseline


def test_readiness_accepts_kubernetes_quantity_without_vm_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KVM_DEVICE_PLUGIN_IMAGE", "example.test/device-plugin:fixture")
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str) -> str:
        calls.append(args)
        assert "clusterpolicy" not in args
        if args[:2] == ("get", "nodes"):
            return json.dumps({"items": [{"status": {"allocatable": {"devices.kubevirt.io/kvm": "1k"}}}]})
        return "condition met"

    monkeypatch.setattr(node, "kubectl", kubectl)
    runtime_setup = runtime.runtime(tmp_path).runtime_setup
    assert runtime_setup is not None
    runtime_setup.wait_until_ready(node)
    assert any(call[:2] == ("get", "nodes") for call in calls)


@pytest.mark.parametrize("deletion_fails", [False, True])
def test_retained_cleanup_certifies_only_the_removed_device_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deletion_fails: bool
) -> None:
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(tmp_path / "outputs"))
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    deleted: list[str] = []

    def kubectl(*args: str) -> str:
        if args[0] == "delete":
            deleted.append(args[2])
            if deletion_fails:
                raise RuntimeError("injected namespace deletion failure")
            return ""
        if "--ignore-not-found=true" in args:
            return ""
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": [{"metadata": {"uid": "fake-plugin-pod"}}]})
        return json.dumps({"metadata": {"uid": "fake-" + args[1]}})

    monkeypatch.setattr(node, "kubectl", kubectl)
    guest = runtime.FirecrackerGuestLifecycle()
    if deletion_fails:
        with pytest.raises(RuntimeError, match="injected namespace deletion failure"):
            guest.clean(node)
    else:
        guest.clean(node)
    receipt = json.loads((tmp_path / "outputs/firecracker-runtime.json").read_text())
    assert deleted == ["kvm-device-plugin"]
    assert receipt["namespace_uid"] == "fake-namespace"
    assert receipt["daemonset_uid"] == "fake-daemonset"
    assert receipt["pod_uids"] == ["fake-plugin-pod"]
    assert receipt["cleanup"] == ("pending" if deletion_fails else "complete")
