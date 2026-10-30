"""The guest config the VM SandboxTemplate carries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import (
    App,
    Chart,
    Testing as Cdk8sTesting,  # not collected by pytest
)
from more_itertools import one

from agentplane.runner.configuration import Configuration, VMEnvironment
from cluster.cdk8s.agentplane import runner_sandbox, runner_vm, staging

HARNESS_ENV = {"EXAMPLE_SERVICE_URL": "http://service.test"}


def _runner_vm_manifests(
    *, app: App, console_autologin: bool, session_reconcile_concurrency: int | None = None
) -> dict[str, dict[str, Any]]:
    chart = Chart(app, "test")
    runner_vm.RunnerVmTemplate(
        chart,
        "runner-vm-template",
        replace(staging.ENV, vm_console_autologin=console_autologin),
        labels={},
        state_volume={
            "name": "state",
            "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
            "layout": {"type": "DirectoryTree"},
        },
        workspace_volume=runner_sandbox.workspace_volume_spec(
            {"claim": {"storageClassName": "test-storage"}}, layout_type="BlockFilesystem"
        ),
        platform_instructions="Test platform guidance.",
        harness_env=HARNESS_ENV,
        session_reconcile_concurrency=session_reconcile_concurrency,
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    config_map = one(
        doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "runner-vm-config"
    )
    sandbox_template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    return {
        "runner": cast(dict[str, Any], json.loads(config_map["data"]["runner.json"])),
        "sandbox_template": sandbox_template,
    }


@pytest.mark.parametrize(("console_autologin", "rendered"), [(True, True), (False, False)])
def test_console_autologin_reaches_the_vm_environment_only_when_enabled(
    scratch_app: App, console_autologin: bool, rendered: bool
) -> None:
    config = Configuration.model_validate(
        _runner_vm_manifests(app=scratch_app, console_autologin=console_autologin)["runner"]
    )
    assert isinstance(config.environment, VMEnvironment)
    assert config.environment.console_autologin == rendered


def test_harness_env_reaches_the_vm_environment(scratch_app: App) -> None:
    rendered = _runner_vm_manifests(app=scratch_app, console_autologin=False)
    runner = Configuration.model_validate(rendered["runner"])
    assert all(runner.harness_environment.values[name] == value for name, value in HARNESS_ENV.items())
    assert runner.session_reconcile_concurrency is None
    assert isinstance(runner.environment, VMEnvironment)


def test_session_reconcile_concurrency_reaches_vm_runner_configuration(scratch_app: App) -> None:
    rendered = _runner_vm_manifests(app=scratch_app, console_autologin=False, session_reconcile_concurrency=2048)
    config = Configuration.model_validate(rendered["runner"])
    assert config.session_reconcile_concurrency == 2048


def test_vm_size_and_runner_configuration_share_resource_policy(scratch_app: App) -> None:
    rendered = _runner_vm_manifests(app=scratch_app, console_autologin=False)
    config = rendered["runner"]["environment"]
    vm = rendered["sandbox_template"]["spec"]["vm"]
    domain = vm["template"]["spec"]["domain"]

    assert domain["cpu"]["cores"] == config["resources"]["guest_vcpu_count"]
    memory = domain["resources"]["requests"]["memory"]
    assert int(memory.removesuffix("Mi")) * 1024 * 1024 == config["resources"]["guest_memory_bytes"]


def test_vm_workspace_is_a_controller_attached_block_filesystem(scratch_app: App) -> None:
    template = _runner_vm_manifests(app=scratch_app, console_autologin=False)["sandbox_template"]["spec"]
    assert template["workspaceVolume"] == {
        "name": "workspace",
        "claim": {"storageClassName": "test-storage", "volumeMode": "Block"},
        "layout": {"type": "BlockFilesystem", "filesystem": "ext4"},
    }
    assert template["stateVolume"] == {
        "name": "state",
        "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
        "layout": {"type": "DirectoryTree"},
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


def test_staging_uses_the_replicated_class_for_both_vm_claims_and_keeps_pod_storage(
    staging_manifests: list[dict[str, Any]],
) -> None:
    vm_template = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "SandboxTemplate" and doc["metadata"]["name"] == runner_vm.NAME
    )
    vm_state = vm_template["spec"]["stateVolume"]
    vm_workspace = vm_template["spec"]["workspaceVolume"]
    assert vm_state["layout"] == {"type": "DirectoryTree"}
    assert vm_state["claim"]["volumeMode"] == "Filesystem"
    assert vm_workspace["claim"]["volumeMode"] == "Block"
    assert vm_workspace["layout"] == {"type": "BlockFilesystem", "filesystem": "ext4"}
    assert vm_state["claim"]["storageClassName"] == vm_workspace["claim"]["storageClassName"]
    # The Piraeus provider declares this class in cluster/cdk8s/piraeus.py.
    assert staging.ENV.vm_storage_class == "piraeus-ssd-2-autodiskful"
    assert vm_state["claim"]["storageClassName"] == staging.ENV.vm_storage_class

    runner_template = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "SandboxTemplate" and doc["metadata"]["name"] == staging.POD_TEMPLATE
    )
    pod_state = runner_template["spec"]["stateVolume"]
    assert pod_state["claim"]["storageClassName"] != vm_state["claim"]["storageClassName"]
    assert runner_template["spec"]["workspaceVolume"]["layout"] == {"type": "DirectoryTree"}


def test_vm_storage_class_requires_vm_sandboxes() -> None:
    with pytest.raises(ValueError, match="vm_storage_class requires vm_sandboxes"):
        replace(staging.ENV, vm_sandboxes=False, vm_storage_class="example-storage")


def test_vm_template_renders_mtls_server_listener(scratch_app: App) -> None:
    rendered = _runner_vm_manifests(app=scratch_app, console_autologin=False)
    runner = rendered["runner"]
    template = rendered["sandbox_template"]["spec"]

    assert runner["control"] == {
        "listen_address": "0.0.0.0:9090",
        "security": {
            "mode": "mtls",
            "tls": {
                "cert_file": "/run/agentplane/runner-tls/tls.crt",
                "key_file": "/run/agentplane/runner-tls/tls.key",
                "ca_file": "/run/agentplane/runner-trust/ca-certificates.crt",
            },
        },
    }
    interface = template["vm"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]
    assert {"port": 9090} in interface["ports"]


if __name__ == "__main__":
    pytest_bazel.main()
