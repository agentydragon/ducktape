"""The guest config the VM SandboxTemplate carries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from agentplane.env.config import CodexReadiness, Configuration, SystemdEnvironment
from cluster.cdk8s.agentplane import environment_guest, environment_vm, launcher_relay, staging
from cluster.cdk8s.testing.manifests import find_resource


@pytest.fixture(scope="module")
def vm_spec(staging_manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return cast(dict[str, Any], find_resource(staging_manifests, "SandboxTemplate", environment_vm.NAME)["spec"])


@pytest.fixture(scope="module")
def guest_config(staging_manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return find_resource(staging_manifests, "ConfigMap", f"{environment_vm.NAME}-config")


@pytest.fixture(scope="module")
def runtime(guest_config: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(guest_config["data"]["environment-runtime.json"]))


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


def test_executor_selects_native_implementation(runtime: dict[str, Any]) -> None:
    environment = runtime["environment"]
    executor = environment["executor"]
    assert executor["implementation"] == "native"
    assert executor["owner"] == {"kind": "systemd"}
    assert executor["placement"] == "systemd"
    assert staging.STAGING_VM_RESOURCES.executor_workload is not None
    assert environment["resources"]["executor_workload"] == staging.STAGING_VM_RESOURCES.executor_workload.model_dump()


def test_default_runtime_settings(runtime: dict[str, Any]) -> None:
    config = Configuration.model_validate(runtime)
    assert config.harness_environment.values["HOME"] == "/state/home"
    assert config.session_reconcile_concurrency is None
    assert config.codex_readiness is None
    assert isinstance(config.environment, SystemdEnvironment)
    assert config.environment.console_autologin is False


def test_runtime_overrides_reach_the_vm_guest() -> None:
    readiness = CodexReadiness(
        exec_server_ready_timeout_ms=60_000,
        exec_server_probe_timeout_ms=5_000,
        exec_server_dependency_timeout_ms=120_000,
    )
    data = environment_guest.guest_config_data(
        replace(staging.ENV, vm_console_autologin=True),
        gateway=launcher_relay.GUEST_GATEWAY,
        block_devices=("/dev/vdb",),
        platform_instructions="Test platform guidance.",
        harness_env={"EXAMPLE_SERVICE_URL": "http://service.test"},
        session_reconcile_concurrency=2048,
        codex_readiness=readiness,
    )
    config = Configuration.model_validate_json(data["environment-runtime.json"])
    assert config.harness_environment.values["EXAMPLE_SERVICE_URL"] == "http://service.test"
    assert isinstance(config.environment, SystemdEnvironment)
    assert config.environment.console_autologin is True
    assert config.session_reconcile_concurrency == 2048
    assert config.codex_readiness == readiness


def test_vm_size_and_environment_runtime_configuration_share_resource_policy(
    vm_spec: dict[str, Any], runtime: dict[str, Any]
) -> None:
    config = runtime["environment"]
    vm = vm_spec["vm"]
    domain = vm["template"]["spec"]["domain"]

    assert domain["cpu"]["cores"] == config["resources"]["guest_vcpu_count"]
    memory = domain["resources"]["requests"]["memory"]
    assert int(memory.removesuffix("Mi")) * 1024 * 1024 == config["resources"]["guest_memory_bytes"]


def test_vm_workspace_is_a_controller_attached_block_filesystem(vm_spec: dict[str, Any]) -> None:
    vm = vm_spec["vm"]
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
    vm_template = find_resource(staging_manifests, "SandboxTemplate", environment_vm.NAME)
    vm_state = vm_template["spec"]["stateVolume"]
    vm_workspace = vm_template["spec"]["workspaceVolume"]
    assert vm_state["layout"] == {"type": "DirectoryTree"}
    assert vm_state["claim"]["volumeMode"] == "Filesystem"
    assert vm_workspace["claim"]["volumeMode"] == "Block"
    assert vm_workspace["layout"] == {"type": "BlockFilesystem", "filesystem": "ext4"}
    assert vm_state["claim"]["storageClassName"] == vm_workspace["claim"]["storageClassName"]
    assert vm_state["claim"]["storageClassName"] == staging.ENV.vm_storage_class

    environment_template = find_resource(staging_manifests, "SandboxTemplate", staging.POD_TEMPLATE)
    pod_state = environment_template["spec"]["stateVolume"]
    assert pod_state["claim"]["storageClassName"] != vm_state["claim"]["storageClassName"]
    assert environment_template["spec"]["workspaceVolume"]["layout"] == {"type": "DirectoryTree"}


def test_vm_storage_class_requires_vm_sandboxes() -> None:
    with pytest.raises(ValueError, match="vm_storage_class requires vm_sandboxes"):
        replace(staging.ENV, vm_sandboxes=False, vm_storage_class="example-storage")


def test_vm_template_renders_mtls_server_listener(vm_spec: dict[str, Any], runtime: dict[str, Any]) -> None:

    assert runtime["control"]["security"]["mode"] == "mtls"
    interface = vm_spec["vm"]["template"]["spec"]["domain"]["devices"]["interfaces"][0]
    listener_port = int(runtime["control"]["listen_address"].rsplit(":", 1)[1])
    assert {"port": listener_port} in interface["ports"]


def test_vm_template_matches_the_guest_disk_and_runtime_file_contract(
    vm_guest_contract: dict[str, Any],
    runtime_service_unit: dict[str, str],
    guest_config: dict[str, Any],
    runtime: dict[str, Any],
    vm_spec: dict[str, Any],
) -> None:
    config_map = guest_config["data"]
    vm = vm_spec["vm"]
    domain = vm["template"]["spec"]["domain"]

    disk_names_by_serial = {disk["serial"]: disk["name"] for disk in domain["devices"]["disks"] if "serial" in disk}
    for disk_name in ("runtime", "config", "egress_trust", "workspace"):
        disk = vm_guest_contract["disks"][disk_name]
        assert disk_names_by_serial[disk["serial"]] == disk["volume_name"]
    volumes_by_name = {volume["name"]: volume for volume in vm["template"]["spec"]["volumes"]}
    assert (
        volumes_by_name[vm_guest_contract["disks"]["config"]["volume_name"]]["configMap"]["name"]
        == guest_config["metadata"]["name"]
    )

    assert runtime["state_dir"] == vm_guest_contract["state"]["mount"]
    assert runtime["control"]["security"]["tls"]["cert_file"] == (
        f"{vm_guest_contract['environment_runtime_tls']['runtime_path']}/tls.crt"
    )
    assert runtime["control"]["security"]["tls"]["key_file"] == (
        f"{vm_guest_contract['environment_runtime_tls']['runtime_path']}/tls.key"
    )
    assert runtime["control"]["security"]["tls"]["ca_file"] == (
        f"{vm_guest_contract['environment_runtime_trust']['mount']}/{vm_guest_contract['environment_runtime_trust']['key']}"
    )
    workload_trust = vm_guest_contract["environment_runtime_workload_trust"]
    assert runtime["control"]["security"]["tls"]["client_ca_file"] == (
        f"{workload_trust['mount']}/{workload_trust['key']}"
    )
    assert runtime["harness_environment"]["values"]["PATH"].endswith(
        f"{vm_guest_contract['disks']['runtime']['mount']}/bin"
    )
    assert runtime["environment"]["block_devices"] == [vm_guest_contract["disks"]["workspace"]["device"]]
    assert vm_guest_contract["files"]["runtime_config"] in config_map
    assert vm_guest_contract["files"]["runtime_environment"] in config_map
    assert "kubeconfig" not in config_map
    assert "KUBECONFIG" not in runtime["harness_environment"]["values"]

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
        vm_guest_contract["environment_runtime_workload_trust"]["mount"],
    }


if __name__ == "__main__":
    pytest_bazel.main()
