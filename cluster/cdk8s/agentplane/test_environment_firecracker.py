"""The opt-in Firecracker template preserves the host/guest security boundary."""

import json
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart
from cdk8s_plus_34 import ConfigMap
from more_itertools import one

from agentplane.egress.platform_routes import PLATFORM_ROUTES_PORT
from agentplane.net.peertls.config import SESSION_DISPATCHER_URI
from cluster.cdk8s.agentplane import environment_firecracker, runner_gateway, staging
from cluster.cdk8s.agentplane.egress import DEFAULT_POLICIES
from cluster.cdk8s.agentplane.kubernetes_account_ref import shared_diagnostics
from cluster.cdk8s.pod_policy import pod_spec
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy


@pytest.fixture(scope="module")
def firecracker(scratch_app_factory: Callable[[], App]) -> environment_firecracker.EnvironmentFirecrackerTemplate:
    return environment_firecracker.EnvironmentFirecrackerTemplate(
        Chart(scratch_app_factory(), "firecracker-test"),
        "template",
        staging.ENV,
        labels={"app": "runner"},
        storage_class="guest-storage",
        runner_image="example.invalid/runner",
    )


@pytest.fixture(scope="module")
def rendered(firecracker: environment_firecracker.EnvironmentFirecrackerTemplate) -> dict[str, Any]:
    return cast(dict[str, Any], firecracker.template.to_json())


@pytest.fixture(scope="module")
def runtime(firecracker: environment_firecracker.EnvironmentFirecrackerTemplate) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(firecracker.configuration.data["environment-runtime.json"]))


def test_firecracker_preserves_host_guest_isolation(
    firecracker: environment_firecracker.EnvironmentFirecrackerTemplate,
    rendered: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    pod = pod_spec(rendered)
    host = one(c for c in pod["containers"] if c["name"] == "environment-runtime")
    relay = one(c for c in pod["initContainers"] if c["name"] == "egress-sidecar")
    assert host["securityContext"]["privileged"] is True
    assert relay["securityContext"]["runAsNonRoot"] is True
    assert relay["securityContext"]["runAsUser"] == 1000
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert host["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 9092}
    guest_volume = one(volume for volume in pod["volumes"] if volume["name"] == "guest-config")
    assert guest_volume["configMap"]["name"] == firecracker.configuration.name
    assert runtime["environment"]["block_devices"] == ["/dev/vdd"]
    policy = one(child for child in firecracker.node.find_all() if isinstance(child, NetworkPolicy))
    ingress = one(policy.to_json()["spec"]["ingress"])
    assert set(ingress["fromEntities"]) == {"kube-apiserver", "host"}
    assert one(one(ingress["toPorts"])["ports"])["port"] == str(host["readinessProbe"]["httpGet"]["port"])


def test_firecracker_native_grpc_urls_use_the_guest_relay_gateway(
    firecracker: environment_firecracker.EnvironmentFirecrackerTemplate,
    runtime: dict[str, Any],
    rendered: dict[str, Any],
) -> None:
    harness = runtime["harness_environment"]["values"]
    config_maps = {child.name: child for child in firecracker.node.find_all() if isinstance(child, ConfigMap)}
    volumes = {v["name"]: v["configMap"]["name"] for v in pod_spec(rendered)["volumes"] if "configMap" in v}
    relay = config_maps[volumes["egress-sidecar-settings"]]
    launcher = config_maps[volumes["firecracker-settings"]]
    relay_settings = yaml.safe_load(relay.data["settings.yaml"])
    proxy = urlsplit(harness["HTTP_PROXY"])
    assert proxy.hostname == environment_firecracker.GATEWAY
    assert relay_settings["listen_address"] == f"[::]:{proxy.port}"
    platform = relay_settings["platform_routes"]
    assert {"environments", "history"} <= platform["routes"].keys()
    assert "/agentplane.llm_ingress.v1.ModelConfigService/" in platform["routes"]["model-config"]["service_prefixes"]
    listener = urlsplit(f"//{platform['listen_address']}")
    assert (listener.hostname, listener.port) == ("0.0.0.0", PLATFORM_ROUTES_PORT)
    assert json.loads(launcher.data["config.json"])["platform_route_ports"] == [listener.port]
    for url in (
        harness["AGENTPLANE_EGRESS_GRPC_URL"],
        harness["AGENTPLANE_HISTORY_URL"],
        harness["AGENTPLANE_ENVIRONMENTS_URL"],
        harness["AGENTPLANE_NOTIFICATIONS_GRPC_URL"],
        f"//{runtime['model_config_target']}",
    ):
        target = urlsplit(url)
        assert (target.hostname, target.port) == (environment_firecracker.GATEWAY, listener.port)
    sessions = runner_gateway.sessions_service(staging.ENV.namespace).authority
    assert harness["AGENTPLANE_SESSIONS_URL"] == f"http://{sessions}"
    assert relay_settings["workload_routes"][sessions] == {"upstream": sessions, "server_uri": SESSION_DISPATCHER_URI}


def test_firecracker_launcher_has_kvm_and_guest_storage(rendered: dict[str, Any]) -> None:
    pod = pod_spec(rendered)
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    launcher = one(container for container in pod["containers"] if container["name"] == "environment-runtime")
    assert launcher["resources"]["limits"]["devices.kubevirt.io/kvm"] == 1
    guest_memory = rendered["spec"]["firecracker"]["memoryMiB"] * 1024**2
    memory_request = launcher["resources"]["requests"]["memory"]
    memory_limit = launcher["resources"]["limits"]["memory"]
    assert memory_request > guest_memory
    assert memory_limit - memory_request >= guest_memory
    for volume in ("stateVolume", "workspaceVolume"):
        assert rendered["spec"][volume]["claim"]["storageClassName"] == "guest-storage"
        assert rendered["spec"][volume]["layout"] == {"type": "RawDiskImage", "filesystem": "ext4"}


def test_firecracker_launch_presets_use_the_guest_workspace() -> None:
    env = staging.ENV
    assert env.app is not None
    assert env.runtime_catalog is not None
    assert env.actions.orchestration_v1 is not None
    preset = env.app.config.sandbox_presets["firecracker"]
    assert preset.template == environment_firecracker.NAME
    assert preset.policy_sets == [*DEFAULT_POLICIES, shared_diagnostics(env.namespace).credential_name]
    thread = env.app.config.thread_presets[preset.thread_preset]
    assert thread.harness == "HARNESS_CODEX"
    runtime = env.runtime_catalog.templates[preset.template]
    assert runtime.workspace_root_enforced
    assert runtime.workspace_root == "/state/workspaces"
    assert thread.cwd == f"{runtime.workspace_root}/{{session_id}}"
    assert env.actions.orchestration_v1.presets["firecracker"].launch.template == preset.template


if __name__ == "__main__":
    pytest_bazel.main()
