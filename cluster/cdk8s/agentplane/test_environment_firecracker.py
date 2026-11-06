"""The opt-in Firecracker template preserves the host/guest security boundary."""

import json
from dataclasses import replace
from typing import Any, cast

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from more_itertools import one

from cluster.cdk8s.agentplane import (
    environment_firecracker,
    environment_sandbox,
    runner_gateway,
    session_dispatcher,
    staging,
)
from cluster.cdk8s.pod_policy import harden, pod_spec


def test_firecracker_mounts_and_tokens_stay_on_the_correct_side(scratch_app: App) -> None:
    chart = Chart(scratch_app, "firecracker-test")
    state = {
        "name": "state",
        "claim": {"storageClassName": "test-storage", "volumeMode": "Filesystem"},
        "layout": {"type": "RawDiskImage", "filesystem": "ext4"},
    }
    template = environment_firecracker.EnvironmentFirecrackerTemplate(
        chart,
        "template",
        replace(staging.ENV, firecracker_sandboxes=True, runner_egress_mtls=True),
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
    assert pod["securityContext"]["fsGroup"] == 1000
    host_mounts = {m["name"] for m in host["volumeMounts"]}
    token_volumes = {v["name"] for v in pod["volumes"] if "projected" in v}
    assert token_volumes
    assert not host_mounts & token_volumes
    assert token_volumes <= {m["name"] for m in relay["volumeMounts"]}
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
    relay_settings = next(iter(relay_config["data"].values()))
    assert "[::]:3128" in relay_settings
    assert "proxy_mode: mtls" in relay_settings
    assert "proxy_port: 8889" in relay_settings
    assert "token_file: /var/run/agentplane-egress/token" in relay_settings
    assert "/run/agentplane/environment-runtime-tls/tls.crt" in relay_settings
    routes = yaml.safe_load(relay_settings)["workload_routes"]
    destination = runner_gateway.workload_service(staging.ENV.namespace)
    for origin in (
        session_dispatcher.service(staging.ENV.namespace),
        session_dispatcher.http_service(staging.ENV.namespace),
    ):
        route = routes[f"{origin.fqdn}:{origin.port.number}"]
        assert route["upstream"] == f"{destination.fqdn}:{destination.pod_port}"
        assert route["server_uri"] == runner_gateway.WORKLOAD_URI
    status_policy = one(doc for doc in docs if doc["kind"] == "CiliumNetworkPolicy")
    assert status_policy["spec"]["ingress"][0]["fromEntities"] == ["kube-apiserver", "host"]
    assert rendered["spec"]["firecracker"]["runnerImage"] == "registry.test/runner@sha256:123"


def test_staging_keeps_firecracker_disabled(staging_manifests: list[dict[str, Any]]) -> None:
    assert not any(doc["kind"] == "SandboxTemplate" and "firecracker" in doc["spec"] for doc in staging_manifests)


if __name__ == "__main__":
    pytest_bazel.main()
