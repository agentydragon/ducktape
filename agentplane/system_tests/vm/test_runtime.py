"""VM cleanup must finish before a retained node becomes reusable."""

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.deployment import rendered_fixture, stack
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.vm import host_qemu, infrastructure as vm_infrastructure, sandboxes as vm_sandboxes
from agentplane.system_tests.vm.runtime import VmRuntime


@pytest.mark.parametrize("leftover", [None, "vm", "storage_class"])
def test_vm_clean_certification_requires_empty_vm_inventory_and_removed_storage_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leftover: str | None
) -> None:
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", tmp_path / "server.json")
    runtime = VmRuntime(tmp_path)
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    marker: dict[str, Any] = {"state": "dirty"}
    baseline.write_marker(marker)
    namespaces_deleted = False
    storage_class_deleted = False

    def clean_workloads(_node: baseline.K3s, _fixture: rendered_fixture.RenderedFixture) -> None:
        nonlocal namespaces_deleted
        namespaces_deleted = True
        assert not runtime.closed

    def kubectl(*args: str) -> str:
        nonlocal storage_class_deleted
        assert namespaces_deleted
        assert not runtime.closed
        if args[:2] == ("get", "vm,vmi"):
            return json.dumps({"items": [{"metadata": {"name": "test-leftover-vm"}}] if leftover == "vm" else []})
        if args[:2] == ("delete", "storageclass"):
            storage_class_deleted = True
            return ""
        if args[:2] == ("get", "storageclass"):
            assert storage_class_deleted
            return "storageclass/test-leftover" if leftover == "storage_class" else ""
        raise AssertionError(args)

    monkeypatch.setattr(stack, "_baseline_clean", clean_workloads)
    monkeypatch.setattr(node, "kubectl", kubectl)
    fixture = rendered_fixture.RenderedFixture(
        backend=rendered_fixture.Backend.LOCAL_MODEL,
        vm_sandboxes=True,
        namespace="systest",
        app_origin="http://app.test.invalid",
        browser_services=[],
        mcp_fixtures=None,
        objects=[],
    )
    with contextlib.ExitStack() as resources, contextlib.ExitStack() as volumes:
        volumes.enter_context(runtime)
        if leftover is not None:
            with pytest.raises(RuntimeError):
                stack._certify_baseline(node, fixture, runtime, marker, resources, volumes)
            assert not runtime.closed
            assert json.loads((tmp_path / "server.json").read_text())["state"] == "dirty"
        else:
            assert stack._certify_baseline(node, fixture, runtime, marker, resources, volumes) == 0
            assert runtime.closed
            assert json.loads((tmp_path / "server.json").read_text())["state"] == "clean"


def test_vm_runtime_skips_qmp_report_probe_for_opted_in_passive_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setenv(host_qemu.VIRTIOFSD_TIMELINE_ENV, "1")
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    monkeypatch.setattr(host_qemu, "snapshot_pending", lambda *_args: calls.append("qmp"))
    monkeypatch.setattr(vm_sandboxes, "report", lambda *_args: "report")

    runtime = VmRuntime(tmp_path)
    k3s = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    assert runtime.report(k3s, "systest") == "report"
    assert calls == []


if __name__ == "__main__":
    pytest_bazel.main()
