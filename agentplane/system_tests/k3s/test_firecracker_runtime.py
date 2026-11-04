"""Firecracker waits for a KVM resource without requiring VM admission policies."""

import json
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.k3s import baseline, vm_infrastructure
from agentplane.system_tests.k3s.firecracker_runtime import FirecrackerRuntime


def test_readiness_accepts_kubernetes_quantity_without_vm_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    calls: list[tuple[str, ...]] = []

    def kubectl(*args: str) -> str:
        calls.append(args)
        assert "clusterpolicy" not in args
        if args[:2] == ("get", "nodes"):
            return json.dumps({"items": [{"status": {"allocatable": {"devices.kubevirt.io/kvm": "1k"}}}]})
        return "condition met"

    monkeypatch.setattr(node, "kubectl", kubectl)
    FirecrackerRuntime(tmp_path).wait_until_ready(node)
    assert any("kubevirt/kubevirt" in call for call in calls)
    assert any(call[:2] == ("get", "nodes") for call in calls)


if __name__ == "__main__":
    pytest_bazel.main()
