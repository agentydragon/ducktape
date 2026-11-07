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
from python.runfiles import Runfiles

from agentplane.env.config import CodexReadiness, Configuration, VMEnvironment
from cluster.cdk8s.agentplane import environment_sandbox, environment_vm, staging

HARNESS_ENV = {"EXAMPLE_SERVICE_URL": "http://service.test"}


def _environment_vm_manifests(
    *,
    app: App,
    console_autologin: bool,
    session_reconcile_concurrency: int | None = None,
    codex_readiness: CodexReadiness | None = None,
) -> dict[str, dict[str, Any]]:
    chart = Chart(app, "test")
    environment_vm.EnvironmentVmTemplate(
        chart,
        "environment-vm-template",
        replace(staging.ENV, vm_console_autologin=console_autologin),
        labels={},
        state_volume={
            "name": "state",
            "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
            "layout": {"type": "DirectoryTree"},
        },
        workspace_volume=environment_sandbox.workspace_volume_spec(
            {"claim": {"storageClassName": "test-storage"}}, layout_type="BlockFilesystem"
        ),
        platform_instructions="Test platform guidance.",
        harness_env=HARNESS_ENV,
        session_reconcile_concurrency=session_reconcile_concurrency,
        codex_readiness=codex_readiness,
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    config_map = one(
        doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "environment-vm-config"
    )
    sandbox_template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    return {
        "config_map": config_map["data"],
        "config_map_name": config_map["metadata"]["name"],
        "environment_runtime": cast(dict[str, Any], json.loads(config_map["data"]["environment-runtime.json"])),
        "sandbox_template": sandbox_template,
    }


@pytest.fixture(scope="module")
def vm_guest_contract(runfiles: Runfiles) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (runfiles.root() / "ducktape/agentplane/env/images/vm_guest_contract.json").read_text(encoding="utf-8")
        ),
    )


@pytest.fixture(scope="module")
def runtime_service_unit(runfiles: Runfiles) -> dict[str, str]:
    contents = (
        runfiles.root() / "ducktape/agentplane/env/images/systemd/agentplane-environment-runtime.service"
    ).read_text(encoding="utf-8")
    return {
        key: value
        for line in contents.splitlines()
        if "=" in line and not line.startswith("#")
        for key, value in [line.split("=", maxsplit=1)]
    }


@pytest.mark.parametrize(("console_autologin", "rendered"), [(True, True), (False, False)])
def test_console_autologin_reaches_the_vm_environment_only_when_enabled(
    scratch_app: App, console_autologin: bool, rendered: bool
) -> None:
    config = Configuration.model_validate(
        _environment_vm_manifests(app=scratch_app, console_autologin=console_autologin)["environment_runtime"]
    )
    assert isinstance(config.environment, VMEnvironment)
    assert config.environment.console_autologin == rendered


def test_harness_env_reaches_the_vm_environment(scratch_app: App) -> None:
    rendered = _environment_vm_manifests(app=scratch_app, console_autologin=False)
    config = Configuration.model_validate(rendered["environment_runtime"])
    assert all(config.harness_environment.values[name] == value for name, value in HARNESS_ENV.items())
    assert config.harness_environment.values["HOME"] == "/state/home"
    assert config.session_reconcile_concurrency is None
    assert config.codex_readiness is None
    assert "codex_readiness" not in rendered["environment_runtime"]
    assert isinstance(config.environment, VMEnvironment)


def test_session_reconcile_concurrency_reaches_vm_environment_runtime_configuration(scratch_app: App) -> None:
    rendered = _environment_vm_manifests(app=scratch_app, console_autologin=False, session_reconcile_concurrency=2048)
    config = Configuration.model_validate(rendered["environment_runtime"])
    assert config.session_reconcile_concurrency == 2048


def test_codex_readiness_reaches_vm_environment_runtime_configuration(scratch_app: App) -> None:
    readiness = CodexReadiness(
        exec_server_ready_timeout_ms=60_000,
        exec_server_probe_timeout_ms=5_000,
        exec_server_dependency_timeout_ms=120_000,
    )
    rendered = _environment_vm_manifests(app=scratch_app, console_autologin=False, codex_readiness=readiness)

    assert rendered["environment_runtime"]["codex_readiness"] == {
        "exec_server_ready_timeout_ms": 60_000,
        "exec_server_probe_timeout_ms": 5_000,
        "exec_server_dependency_timeout_ms": 120_000,
    }


def test_vm_size_and_environment_runtime_configuration_share_resource_policy(scratch_app: App) -> None:
    rendered = _environment_vm_manifests(app=scratch_app, console_autologin=False)
    config = rendered["environment_runtime"]["environment"]
    vm = rendered["sandbox_template"]["spec"]["vm"]
    domain = vm["template"]["spec"]["domain"]

    assert domain["cpu"]["cores"] == config["resources"]["guest_vcpu_count"]
    memory = domain["resources"]["requests"]["memory"]
    assert int(memory.removesuffix("Mi")) * 1024 * 1024 == config["resources"]["guest_memory_bytes"]


def test_vm_workspace_is_a_controller_attached_block_filesystem(scratch_app: App) -> None:
    template = _environment_vm_manifests(app=scratch_app, console_autologin=False)["sandbox_template"]["spec"]
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
        if doc["kind"] == "SandboxTemplate" and doc["metadata"]["name"] == environment_vm.NAME
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

    environment_template = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "SandboxTemplate" and doc["metadata"]["name"] == staging.POD_TEMPLATE
    )
    pod_state = environment_template["spec"]["stateVolume"]
    assert pod_state["claim"]["storageClassName"] != vm_state["claim"]["storageClassName"]
    assert environment_template["spec"]["workspaceVolume"]["layout"] == {"type": "DirectoryTree"}


def test_vm_storage_class_requires_vm_sandboxes() -> None:
    with pytest.raises(ValueError, match="vm_storage_class requires vm_sandboxes"):
        replace(staging.ENV, vm_sandboxes=False, vm_storage_class="example-storage")


def test_vm_template_renders_mtls_server_listener(scratch_app: App) -> None:
    rendered = _environment_vm_manifests(app=scratch_app, console_autologin=False)
    environment_runtime = rendered["environment_runtime"]
    template = rendered["sandbox_template"]["spec"]

    assert environment_runtime["control"]["security"]["mode"] == "mtls"
    interface = template["vm"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]
    listener_port = int(environment_runtime["control"]["listen_address"].rsplit(":", 1)[1])
    assert {"port": listener_port} in interface["ports"]


def test_vm_guest_config_uses_the_local_model_config_route(scratch_app: App) -> None:
    runtime = _environment_vm_manifests(app=scratch_app, console_autologin=False)["environment_runtime"]
    assert runtime["model_config_service"] == {"target": "10.0.2.1:3135", "bearer": "platform-route"}


def test_vm_template_matches_the_guest_disk_and_runtime_file_contract(
    vm_guest_contract: dict[str, Any], runtime_service_unit: dict[str, str], scratch_app: App
) -> None:
    rendered = _environment_vm_manifests(app=scratch_app, console_autologin=False)
    config_map = rendered["config_map"]
    runtime_config = rendered["environment_runtime"]
    vm = rendered["sandbox_template"]["spec"]["vm"]
    domain = vm["template"]["spec"]["domain"]

    disk_names_by_serial = {disk["serial"]: disk["name"] for disk in domain["devices"]["disks"] if "serial" in disk}
    for disk_name in ("runtime", "config", "egress_trust", "workspace"):
        disk = vm_guest_contract["disks"][disk_name]
        assert disk_names_by_serial[disk["serial"]] == disk["volume_name"]
    volumes_by_name = {volume["name"]: volume for volume in vm["template"]["spec"]["volumes"]}
    assert (
        volumes_by_name[vm_guest_contract["disks"]["config"]["volume_name"]]["configMap"]["name"]
        == rendered["config_map_name"]
    )

    assert runtime_config["state_dir"] == vm_guest_contract["state"]["mount"]
    assert runtime_config["control"]["security"]["tls"]["cert_file"] == (
        f"{vm_guest_contract['environment_runtime_tls']['runtime_path']}/tls.crt"
    )
    assert runtime_config["control"]["security"]["tls"]["key_file"] == (
        f"{vm_guest_contract['environment_runtime_tls']['runtime_path']}/tls.key"
    )
    assert runtime_config["control"]["security"]["tls"]["ca_file"] == (
        f"{vm_guest_contract['environment_runtime_trust']['mount']}/{vm_guest_contract['environment_runtime_trust']['key']}"
    )
    assert runtime_config["harness_environment"]["values"]["PATH"].endswith(
        f"{vm_guest_contract['disks']['runtime']['mount']}/bin"
    )
    assert runtime_config["environment"]["block_devices"] == [vm_guest_contract["disks"]["workspace"]["device"]]
    assert vm_guest_contract["files"]["runtime_config"] in config_map
    assert vm_guest_contract["files"]["runtime_environment"] in config_map
    assert "kubeconfig" not in config_map
    assert "KUBECONFIG" not in runtime_config["harness_environment"]["values"]

    config_mount = vm_guest_contract["disks"]["config"]["mount"]
    launch_mount = vm_guest_contract["disks"]["launch"]["mount"]
    runtime_mount = vm_guest_contract["disks"]["runtime"]["mount"]
    assert (
        runtime_service_unit["EnvironmentFile"] == f"{config_mount}/{vm_guest_contract['files']['runtime_environment']}"
    )
    assert runtime_service_unit["ExecStart"] == (
        f"{runtime_mount}/bin/agentplane-environment-runtime"
        f" --config {config_mount}/{vm_guest_contract['files']['runtime_config']}"
        f" --launch-file {launch_mount}/{vm_guest_contract['files']['launch']}"
    )
    assert set(runtime_service_unit["RequiresMountsFor"].split()) == {
        vm_guest_contract["state"]["mount"],
        vm_guest_contract["disks"]["workspace"]["mount"],
        runtime_mount,
        config_mount,
        launch_mount,
        vm_guest_contract["disks"]["egress_trust"]["mount"],
        vm_guest_contract["environment_runtime_tls"]["private_mount"],
        vm_guest_contract["environment_runtime_trust"]["mount"],
    }


if __name__ == "__main__":
    pytest_bazel.main()
