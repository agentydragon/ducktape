"""The guest config the VM SandboxTemplate carries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import (
    Chart,
    Testing as Cdk8sTesting,  # not collected by pytest
)
from more_itertools import one

from cluster.cdk8s.agentplane import runner_vm, testing


def _runner_vm_manifests(*, console_autologin: bool) -> dict[str, dict[str, Any]]:
    chart = Chart(Cdk8sTesting.app(), "test")
    runner_vm.RunnerVmTemplate(
        chart,
        "runner-vm-template",
        replace(testing.ENV, vm_console_autologin=console_autologin),
        labels={},
        state_volume={"claim": {"storageClassName": "test-storage"}},
        workspace_volume={
            "name": "workspace",
            "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
            "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
        },
        platform_instructions="Test platform guidance.",
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    config_map = one(doc for doc in docs if doc["kind"] == "ConfigMap")
    sandbox_template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    return {
        "config": cast(dict[str, Any], json.loads(config_map["data"]["config.json"])),
        "sandbox_template": sandbox_template,
    }


def _guest_config(*, console_autologin: bool) -> dict[str, Any]:
    return _runner_vm_manifests(console_autologin=console_autologin)["config"]


@pytest.mark.parametrize(("console_autologin", "rendered"), [(True, True), (False, None)])
def test_console_autologin_reaches_the_guest_config_only_when_enabled(
    console_autologin: bool, rendered: bool | None
) -> None:
    # An image that predates the field rejects it, so a disabled environment must not render it.
    assert _guest_config(console_autologin=console_autologin).get("console_autologin") == rendered


def test_vm_workspace_is_a_controller_attached_raw_disk_image() -> None:
    template = _runner_vm_manifests(console_autologin=False)["sandbox_template"]["spec"]
    assert template["workspaceVolume"] == {
        "name": "workspace",
        "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
        "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
    }
    vm = template["vm"]
    disks = vm["template"]["spec"]["domain"]["devices"]["disks"]
    assert any(
        disk["name"] == "workspace" and disk["disk"]["bus"] == "virtio" and disk["serial"] == "workspace"
        for disk in disks
    )
    volumes = vm["template"]["spec"]["volumes"]
    assert "workspace" not in {volume["name"] for volume in volumes}
    assert "dataVolumeTemplates" not in vm


if __name__ == "__main__":
    pytest_bazel.main()
