"""VM cleanup must finish before a retained node becomes reusable."""

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.deployment import rendered_fixture, stack
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.vm import (
    boot_rcu,
    host_qemu,
    infrastructure as vm_infrastructure,
    sandboxes as vm_sandboxes,
)
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


def _vm_template(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "agentplane.allegedly.works/v1alpha1",
        "kind": "SandboxTemplate",
        "metadata": {"name": name},
        "spec": {
            "vm": {"template": {"spec": {"domain": {"firmware": {"kernelBoot": {"kernelArgs": "console=ttyS0"}}}}}}
        },
    }


def test_boot_rcu_observer_is_off_by_default_for_all_full_suite_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTPLANE_VM_BOOT_RCU_OBSERVER", raising=False)
    monkeypatch.delenv("AGENTPLANE_VM_BOOT_EVIDENCE", raising=False)
    monkeypatch.delenv("AGENTPLANE_VM_LIBVIRT_MONITOR_DEBUG", raising=False)
    monkeypatch.delenv("AGENTPLANE_VM_NATIVE_PROFILE", raising=False)
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    objects = [_vm_template("base"), _vm_template("oom"), {"kind": "ConfigMap", "metadata": {"name": "other"}}]

    runtime = VmRuntime(tmp_path)
    runtime.configure(objects)

    assert runtime._boot_rcu_observer is False
    assert [
        obj["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] for obj in objects[:2]
    ] == ["console=ttyS0", "console=ttyS0"]


def test_boot_rcu_observer_opt_in_mutates_each_vm_template_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTPLANE_VM_BOOT_RCU_OBSERVER", "1")
    monkeypatch.delenv("AGENTPLANE_VM_BOOT_EVIDENCE", raising=False)
    monkeypatch.delenv("AGENTPLANE_VM_LIBVIRT_MONITOR_DEBUG", raising=False)
    monkeypatch.delenv("AGENTPLANE_VM_NATIVE_PROFILE", raising=False)
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    objects = [_vm_template("base"), _vm_template("oom")]
    runtime = VmRuntime(tmp_path)

    runtime.configure(objects)
    runtime.configure(objects)

    args = [obj["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]["kernelArgs"] for obj in objects]
    for value in args:
        assert value.split().count(vm_sandboxes.BOOT_RCU_OBSERVER_ARG) == 1
        assert value.split().count(vm_sandboxes.BOOT_RCU_WINDOW_ARG) == 1
    assert runtime._boot_rcu_observer is True


def test_boot_rcu_observer_rejects_malformed_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPLANE_VM_BOOT_RCU_OBSERVER", "true")
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    runtime = VmRuntime(tmp_path)

    with pytest.raises(ValueError, match="AGENTPLANE_VM_BOOT_RCU_OBSERVER"):
        runtime.configure([_vm_template("base")])


@pytest.mark.parametrize("enabled", [False, True])
def test_boot_rcu_capture_lifecycle_is_opt_in_and_wraps_host_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    events: list[str] = []
    monkeypatch.setenv("AGENTPLANE_VM_BOOT_RCU_OBSERVER", "1" if enabled else "0")
    monkeypatch.delenv(host_qemu.VIRTIOFSD_TIMELINE_ENV, raising=False)
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    monkeypatch.setattr("agentplane.system_tests.vm.runtime.undeclared_outputs_dir", lambda: tmp_path)

    @contextlib.contextmanager
    def fake_capture(name: str):
        events.append(f"{name}:enter")
        try:
            yield
        finally:
            events.append(f"{name}:exit")

    monkeypatch.setattr(
        "agentplane.system_tests.vm.runtime.guest_logs.capture", lambda **_kwargs: fake_capture("guest")
    )
    monkeypatch.setattr(
        "agentplane.system_tests.vm.runtime.host_qemu.capture", lambda *_args, **_kwargs: fake_capture("qemu")
    )
    boot_capture_calls: list[str] = []

    def fake_boot_capture(*_args, **_kwargs):
        boot_capture_calls.append("capture")
        return fake_capture("boot")

    monkeypatch.setattr(boot_rcu, "capture_namespace", fake_boot_capture)
    monkeypatch.setattr(boot_rcu, "summarize_guest_serials", lambda *_args: events.append("guest-summary"))
    runtime = VmRuntime(tmp_path)
    runtime.configure([_vm_template("base")])
    k3s = baseline.K3s(tmp_path, tmp_path / "unused-binary")

    with runtime.capture(k3s, "scope", lambda: True):
        events.append("body")

    assert ("boot:enter" in events) is enabled
    assert ("guest-summary" in events) is enabled
    assert bool(boot_capture_calls) is enabled
    assert events.index("guest:enter") < events.index("qemu:enter")
    assert events.index("qemu:exit") < events.index("guest:exit")
    if enabled:
        assert events.index("guest:enter") < events.index("boot:enter") < events.index("qemu:enter")
        assert events.index("qemu:exit") < events.index("boot:exit") < events.index("guest:exit")
        assert events[-1] == "guest-summary"


@pytest.mark.parametrize(("body_fails", "marker_fails"), [(False, False), (True, False), (True, True)])
def test_boot_rcu_summary_failure_does_not_mask_test_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_fails: bool, marker_fails: bool
) -> None:
    monkeypatch.setenv("AGENTPLANE_VM_BOOT_RCU_OBSERVER", "1")
    monkeypatch.delenv(host_qemu.VIRTIOFSD_TIMELINE_ENV, raising=False)
    monkeypatch.setattr(vm_infrastructure, "releases", list)
    monkeypatch.setattr("agentplane.system_tests.vm.runtime.undeclared_outputs_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "agentplane.system_tests.vm.runtime.guest_logs.capture", lambda **_kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        "agentplane.system_tests.vm.runtime.host_qemu.capture", lambda *_args, **_kwargs: contextlib.nullcontext()
    )
    monkeypatch.setattr(boot_rcu, "capture_namespace", lambda *_args, **_kwargs: contextlib.nullcontext())

    def fail_summary(*_args: Any) -> None:
        raise ValueError("summary detail is deliberately not copied to artifacts")

    monkeypatch.setattr(boot_rcu, "summarize_guest_serials", fail_summary)
    if marker_fails:
        monkeypatch.setattr(boot_rcu, "record_guest_summary_failure", fail_summary)

    runtime = VmRuntime(tmp_path)
    runtime.configure([_vm_template("base")])
    k3s = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    expected_error = RuntimeError if body_fails else ValueError

    def exercise_capture() -> None:
        with runtime.capture(k3s, "scope", lambda: True):
            if body_fails:
                raise RuntimeError("primary shutdown/test failure")

    with pytest.raises(expected_error) as raised:
        exercise_capture()

    if body_fails:
        assert str(raised.value) == "primary shutdown/test failure"
        assert raised.value.__notes__
        assert "guest-summary generation failed" in raised.value.__notes__[0]
        if marker_fails:
            assert "writing its marker also failed" in raised.value.__notes__[0]
        else:
            marker = json.loads((tmp_path / "vm-artifacts" / "boot-rcu-guest-summary-error.json").read_text())
            assert marker["status"] == "failed"
            assert marker["error_type"] == "ValueError"
            assert "summary detail" not in json.dumps(marker)
    else:
        assert "summary detail" in str(raised.value)
        marker = json.loads((tmp_path / "vm-artifacts" / "boot-rcu-guest-summary-error.json").read_text())
        assert marker["status"] == "failed"
        assert marker["error_type"] == "ValueError"


if __name__ == "__main__":
    pytest_bazel.main()
