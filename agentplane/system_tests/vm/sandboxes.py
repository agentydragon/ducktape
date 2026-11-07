"""Per-action VM guest configuration and diagnostics."""

from collections.abc import Callable
from typing import Any

from more_itertools import one

from agentplane.system_tests.vm.configuration import OOM_TEMPLATE

Kubectl = Callable[..., str]
_PROFILE_ROOT_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-vm-guest-root-profile:unset"
BOOT_RCU_OBSERVER_ARG = "agentplane.boot-rcu-observe=1"
BOOT_RCU_WINDOW_ARG = "agentplane.boot-rcu-observe-window=360"


def select_native_profile(objects: list[dict[str, Any]], *, enabled: bool, profile_root: bool) -> None:
    """Select the manual profile root and opt-in sampler inside the VM test action."""
    if enabled and not profile_root:
        raise ValueError("native profiling requires //agentplane/system_tests/suites:test_vm_profile")
    if not enabled and not profile_root:
        return
    oom_template = one(
        obj for obj in objects if obj["kind"] == "SandboxTemplate" and obj["metadata"]["name"] == OOM_TEMPLATE
    )
    if enabled:
        boot = oom_template["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]
        boot["kernelArgs"] += " agentplane.native-profile=1"
    if profile_root:
        for template in (obj for obj in objects if obj["kind"] == "SandboxTemplate" and "vm" in obj["spec"]):
            spec = template["spec"]["vm"]["template"]["spec"]
            root = one(disk for disk in spec["domain"]["devices"]["disks"] if "bootOrder" in disk)
            volume = one(volume for volume in spec["volumes"] if volume["name"] == root["name"])
            volume["containerDisk"]["image"] = _PROFILE_ROOT_IMAGE


def select_boot_evidence(objects: list[dict[str, Any]], *, enabled: bool) -> None:
    """Select retained device-discovery evidence on the OOM guest in the VM system test."""
    if not enabled:
        return
    template = one(
        obj for obj in objects if obj["kind"] == "SandboxTemplate" and obj["metadata"]["name"] == OOM_TEMPLATE
    )
    boot = template["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]
    boot["kernelArgs"] += " agentplane.boot-evidence=1"


def select_boot_rcu_observer(objects: list[dict[str, Any]], *, enabled: bool) -> None:
    """Enable a bounded 360-sample boot observer on every test VM template."""
    if not enabled:
        return
    for template in (obj for obj in objects if obj["kind"] == "SandboxTemplate" and "vm" in obj["spec"]):
        boot = template["spec"]["vm"]["template"]["spec"]["domain"]["firmware"]["kernelBoot"]
        args = boot["kernelArgs"].split()
        for argument in (BOOT_RCU_OBSERVER_ARG, BOOT_RCU_WINDOW_ARG):
            if argument not in args:
                args.append(argument)
        boot["kernelArgs"] = " ".join(args)


def select_libvirt_monitor_debug(objects: list[dict[str, Any]], *, enabled: bool) -> None:
    """Enable libvirt QEMU-monitor debug logs only on this test action's VMs."""
    if not enabled:
        return
    for template in (obj for obj in objects if obj["kind"] == "SandboxTemplate" and "vm" in obj["spec"]):
        metadata = template["spec"]["vm"]["template"].setdefault("metadata", {})
        metadata.setdefault("labels", {})["debugLogs"] = "true"
        metadata.setdefault("annotations", {})["kubevirt.io/libvirt-log-filters"] = "1:qemu.qemu_monitor 3:*"


def report(kubectl: Kubectl, namespace: str) -> str:
    """KubeVirt's and Kyverno's view of the VMs, each guest's serial console, and what Pods pulled from
    registries rather than from the imported layouts."""
    parts = [
        kubectl("get", "kubevirt,vm,vmi,clusterpolicy", "-A", "-o", "yaml", check=False),
        kubectl("get", "events", "-A", "--field-selector=reason=Pulling", check=False),
    ]
    launchers = kubectl("get", "pods", "-n", namespace, "-l", "kubevirt.io=virt-launcher", "-o", "name", check=False)
    for launcher in launchers.split():
        parts.append(f"--- {launcher} guest console")
        parts.append(kubectl("logs", "-n", namespace, launcher, "-c", "guest-console-log", check=False))
    return "\n".join(parts)
