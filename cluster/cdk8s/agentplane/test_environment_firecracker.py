"""The opt-in Firecracker template preserves the host/guest security boundary."""

import json
from dataclasses import replace
from typing import Any, cast
from urllib.parse import urlsplit

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.environment_runtime_transport.config import HISTORY_SERVICE_URI
from cluster.cdk8s.agentplane import environment_firecracker, environment_sandbox, history_service, staging
from cluster.cdk8s.pod_policy import harden, pod_spec


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
    guest_config = one(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "environment-firecracker-config"
    )
    runtime = json.loads(guest_config["data"]["environment-runtime.json"])
    assert runtime["environment"]["block_devices"] == ["/dev/vdd"]
    assert runtime["harness_environment"]["values"]["HTTP_PROXY"] == "http://192.0.2.1:3128"
    relay_config = one(
        doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "environment-firecracker-relay"
    )
    relay_settings = yaml.safe_load(relay_config["data"]["settings.yaml"])
    assert relay_settings["listen_address"] == "[::]:3128"
    platform = relay_settings["platform_routes"]
    listener = urlsplit(f"//{platform['listen_address']}")
    target = urlsplit(f"//{runtime['model_config_target']}")
    assert listener.hostname == "0.0.0.0"
    assert (target.hostname, target.port) == (environment_firecracker.GATEWAY, listener.port)
    launcher_config = one(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "environment-firecracker-launcher"
    )
    assert json.loads(launcher_config["data"]["config.json"])["platform_route_ports"] == [listener.port]
    assert "/agentplane.llm_ingress.v1.ModelConfigService/" in platform["routes"]["model-config"]["service_prefixes"]
    status_policy = one(doc for doc in docs if doc["kind"] == "CiliumNetworkPolicy")
    assert status_policy["spec"]["ingress"][0]["fromEntities"] == ["kube-apiserver", "host"]
    assert rendered["spec"]["firecracker"]["runnerImage"] == "registry.test/runner@sha256:123"


def test_firecracker_native_grpc_urls_use_the_guest_relay_gateway(scratch_app: App) -> None:
    chart = Chart(scratch_app, "firecracker-grpc-routes")
    environment_sandbox.EnvironmentSandbox(
        chart,
        "environment-sandbox",
        replace(staging.ENV, firecracker_sandboxes=True),
        name="runner",
        image="registry.test/runner@sha256:123",
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    config_map = one(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "environment-firecracker-config"
    )
    runtime = json.loads(config_map["data"]["environment-runtime.json"])
    harness = runtime["harness_environment"]["values"]
    relay = one(
        doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "environment-firecracker-relay"
    )
    listener = urlsplit(f"//{yaml.safe_load(relay['data']['settings.yaml'])['platform_routes']['listen_address']}")
    for url in (
        harness["AGENTPLANE_EGRESS_GRPC_URL"],
        harness["AGENTPLANE_NOTIFICATIONS_GRPC_URL"],
        f"//{runtime['model_config_target']}",
    ):
        target = urlsplit(url)
        assert (target.hostname, target.port) == (environment_firecracker.GATEWAY, listener.port)
    history = history_service.service(staging.ENV.namespace)
    origin = f"{history.fqdn}:{history.port.number}"
    assert harness["AGENTPLANE_HISTORY_URL"] == f"http://{origin}"
    route = yaml.safe_load(relay["data"]["settings.yaml"])["workload_routes"][origin]
    assert route == {"upstream": f"{history.fqdn}:{history.port.number}", "server_uri": HISTORY_SERVICE_URI}


def test_staging_keeps_firecracker_disabled(staging_manifests: list[dict[str, Any]]) -> None:
    assert not any(doc["kind"] == "SandboxTemplate" and "firecracker" in doc["spec"] for doc in staging_manifests)


if __name__ == "__main__":
    pytest_bazel.main()
