"""The opt-in Firecracker template preserves the host/guest security boundary."""

import json
from dataclasses import replace
from typing import Any, cast
from urllib.parse import urlsplit

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.egress.platform_routes import PLATFORM_ROUTES_PORT
from agentplane.net.peertls.config import HISTORY_SERVICE_URI, SANDBOX_CONTROLLER_URI, SESSION_DISPATCHER_URI
from agentplane.session_instructions.platform_instructions import SessionRuntime
from cluster.cdk8s.agentplane import environment_firecracker, environment_sandbox, history_service, staging
from cluster.cdk8s.agentplane.egress import DEFAULT_POLICIES
from cluster.cdk8s.agentplane.kubernetes_account_ref import shared_diagnostics
from cluster.cdk8s.agentplane.platform_instructions import agent_cli_environment, platform_instructions, service_urls
from cluster.cdk8s.pod_policy import harden, pod_spec
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


def test_firecracker_mounts_stay_on_the_correct_side(scratch_app: App) -> None:
    chart = Chart(scratch_app, "firecracker-test")
    state = {
        "name": "state",
        "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
        "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
    }
    template = environment_firecracker.EnvironmentFirecrackerTemplate(
        chart,
        "template",
        replace(staging.ENV, firecracker_sandboxes=True),
        labels={"app.kubernetes.io/name": "agentplane-environment-runtime"},
        state_volume=state,
        workspace_volume=environment_sandbox.workspace_volume_spec(state, layout_type="RawDiskImage"),
        runner_image="registry.test/runner@sha256:123",
        platform_instructions="test platform",
        harness_env={"EXAMPLE": "hello"},
    )
    # Fleet hardening must reach the Firecracker pod without overwriting its privileged VMM.
    harden(template.template)
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    rendered = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    pod = pod_spec(rendered)
    host = one(c for c in pod["containers"] if c["name"] == "environment-runtime")
    relay = one(c for c in pod["initContainers"] if c["name"] == "egress-sidecar")
    assert host["securityContext"]["privileged"] is True
    assert relay["securityContext"]["runAsNonRoot"] is True
    assert relay["securityContext"]["runAsUser"] == 1000
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert host["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": 9092}
    guest_config = find_resource(docs, "ConfigMap", "environment-firecracker-config")
    runtime = json.loads(guest_config["data"]["environment-runtime.json"])
    assert runtime["environment"]["block_devices"] == ["/dev/vdd"]
    assert runtime["harness_environment"]["values"]["HTTP_PROXY"] == "http://192.0.2.1:3128"
    relay_config = find_resource(docs, "ConfigMap", "environment-firecracker-relay")
    relay_settings = yaml.safe_load(relay_config["data"]["settings.yaml"])
    assert relay_settings["listen_address"] == "[::]:3128"
    platform = relay_settings["platform_routes"]
    listener = urlsplit(f"//{platform['listen_address']}")
    target = urlsplit(f"//{runtime['model_config_target']}")
    assert listener.hostname == "0.0.0.0"
    assert (target.hostname, target.port) == (environment_firecracker.GATEWAY, listener.port)
    launcher_config = find_resource(docs, "ConfigMap", "environment-firecracker-launcher")
    assert json.loads(launcher_config["data"]["config.json"])["platform_route_ports"] == [listener.port]
    assert "/agentplane.llm_ingress.v1.ModelConfigService/" in platform["routes"]["model-config"]["service_prefixes"]
    status_policy = one(doc for doc in docs if doc["kind"] == "CiliumNetworkPolicy")
    assert status_policy["spec"]["ingress"][0]["fromEntities"] == ["kube-apiserver", "host"]
    assert rendered["spec"]["firecracker"]["runnerImage"] == "registry.test/runner@sha256:123"


def test_firecracker_native_grpc_urls_use_the_guest_relay_gateway(scratch_app: App) -> None:
    chart = Chart(scratch_app, "firecracker-grpc-routes")
    env = replace(staging.ENV, firecracker_sandboxes=True)
    pod_state: dict[str, Any] = {
        "name": "state",
        "claim": {
            "storageClassName": "piraeus-hdd-local",
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "10Gi"}},
        },
        "layout": {"type": "DirectoryTree"},
    }
    firecracker_state: dict[str, Any] = {
        **pod_state,
        "claim": {**pod_state["claim"], "volumeMode": "Filesystem", "resources": {"requests": {"storage": "100Gi"}}},
        "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
    }
    firecracker_workspace = environment_sandbox.workspace_volume_spec(firecracker_state, layout_type="RawDiskImage")
    firecracker_workspace["claim"]["resources"] = pod_state["claim"]["resources"]
    environment_firecracker.EnvironmentFirecrackerTemplate(
        chart,
        "firecracker-template",
        env,
        labels={"app.kubernetes.io/name": "agentplane-environment-runtime"},
        state_volume=firecracker_state,
        workspace_volume=firecracker_workspace,
        runner_image="registry.test/runner@sha256:123",
        platform_instructions=platform_instructions(
            env, session_runtime=SessionRuntime.NATIVE_HARNESS, egress_host=environment_firecracker.GATEWAY
        ),
        harness_env=agent_cli_environment(service_urls(env, egress_host=environment_firecracker.GATEWAY)),
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    config_map = find_resource(docs, "ConfigMap", "environment-firecracker-config")
    runtime = json.loads(config_map["data"]["environment-runtime.json"])
    harness = runtime["harness_environment"]["values"]
    relay = find_resource(docs, "ConfigMap", "environment-firecracker-relay")
    listener = urlsplit(f"//{yaml.safe_load(relay['data']['settings.yaml'])['platform_routes']['listen_address']}")
    for url in (
        harness["AGENTPLANE_EGRESS_GRPC_URL"],
        harness["AGENTPLANE_ENVIRONMENTS_URL"],
        harness["AGENTPLANE_NOTIFICATIONS_GRPC_URL"],
        f"//{runtime['model_config_target']}",
    ):
        target = urlsplit(url)
        assert (target.hostname, target.port) == (environment_firecracker.GATEWAY, listener.port)
    history = history_service.service(staging.ENV.namespace)
    origin = f"{history.fqdn}:{history.port.number}"
    assert listener.port == PLATFORM_ROUTES_PORT
    assert harness["AGENTPLANE_HISTORY_URL"] == f"http://{environment_firecracker.GATEWAY}:{listener.port}"
    relay_settings = yaml.safe_load(relay["data"]["settings.yaml"])
    suffix = f"{env.namespace}.svc.cluster.local"
    sessions = f"sessions.{suffix}:8080"
    assert harness["AGENTPLANE_SESSIONS_URL"] == f"http://{sessions}"
    assert relay_settings["workload_routes"][sessions] == {"upstream": sessions, "server_uri": SESSION_DISPATCHER_URI}
    assert f"session-dispatcher.{suffix}:8080" not in relay_settings["workload_routes"]
    assert relay_settings["platform_routes"]["routes"]["environments"] == {
        "service_prefixes": ["/agentplane.environments.v1.EnvironmentService/"],
        "upstream": f"environments.{suffix}:8082",
        "transport": {"mode": "tls_connect", "server_uri": SANDBOX_CONTROLLER_URI},
    }
    assert origin not in relay_settings["workload_routes"]
    route = relay_settings["platform_routes"]["routes"]["history"]
    assert route == {
        "service_prefixes": ["/agentplane.history_service.HistoryService/"],
        "upstream": f"{history.fqdn}:{history.pod_port}",
        "transport": {"mode": "tls_connect", "server_uri": HISTORY_SERVICE_URI},
    }


def test_staging_exposes_firecracker_in_both_launch_paths(staging_manifests: list[dict[str, Any]]) -> None:
    name = environment_firecracker.NAME
    template = find_resource(staging_manifests, "SandboxTemplate", name)
    pod = pod_spec(template)
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    launcher = one(container for container in pod["containers"] if container["name"] == "environment-runtime")
    assert launcher["resources"]["limits"]["devices.kubevirt.io/kvm"] == 1
    assert template["spec"]["stateVolume"]["claim"]["resources"]["requests"]["storage"] == "100Gi"
    for volume in ("stateVolume", "workspaceVolume"):
        assert template["spec"][volume]["claim"]["storageClassName"] == staging.ENV.vm_storage_class
        assert template["spec"][volume]["layout"] == {"type": "RawDiskImage", "filesystem": "ext4"}

    config = config_map_yaml(staging_manifests, "environments-settings", "config.yaml")
    runtime = config["environment_service"]["runtime_catalog"]["templates"][name]
    assert runtime["workspace_root_enforced"] is True
    assert runtime["workspace_root"] == "/state/workspaces"

    app = config_map_yaml(staging_manifests, "app-config", "config.yaml")
    preset = app["sandbox_presets"]["firecracker"]
    assert preset["template"] == name
    assert preset["policy_sets"] == [*DEFAULT_POLICIES, shared_diagnostics(staging.ENV.namespace).credential_name]
    thread = app["thread_presets"][preset["thread_preset"]]
    assert thread["harness"] == "HARNESS_CODEX"
    assert thread["model"] in app["models"]["harnesses"][thread["harness"]]
    assert thread["cwd"] == "/state/workspaces/{session_id}"

    actions = config_map_yaml(staging_manifests, "actions-settings", "settings.yaml")
    action_preset = actions["action_groups"]["orchestration_v1"]["executor"]["presets"]["firecracker"]
    assert action_preset["launch"]["template"] == name
    assert action_preset["launch"]["policy_sets"] == preset["policy_sets"]
    assert action_preset["create_session"]["spec"]["harness"] == thread["harness"]
    assert action_preset["create_session"]["spec"]["model"] == thread["model"]


if __name__ == "__main__":
    pytest_bazel.main()
