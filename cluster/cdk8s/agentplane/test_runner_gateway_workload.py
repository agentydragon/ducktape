"""The Gateway keeps stable Service addresses for one authenticated gRPC listener."""

from pathlib import PurePosixPath
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.runner_gateway.settings import Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import (
    app,
    notifications,
    runner_gateway,
    runner_gateway_workload,
    sandbox_controller,
    session_dispatcher,
    staging,
)
from cluster.cdk8s.agentplane.conftest import NAMESPACES
from cluster.cdk8s.secret_ref import SecretRef
from util.settings_contract import env_name


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _gateway_settings(documents: list[dict[str, Any]], container: dict[str, Any]) -> dict[str, Any]:
    """Each setting as the Gateway reads it: the container's literal env var, else the mounted file's key."""
    file = yaml.safe_load(_resource(documents, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"])
    env = {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}
    return {
        **file,
        **{
            field: env[env_name(Settings, field)] for field in Settings.model_fields if env_name(Settings, field) in env
        },
    }


def _gateway_objects() -> list[dict[str, Any]]:
    env = staging.ENV
    chart = Chart(Cdk8sTesting.app(), "candidate", disable_resource_name_hashes=True)
    callers = {
        ServiceAccountRef(namespace=env.namespace, name=app.NAME): app.service(env.namespace).pods,
        ServiceAccountRef(namespace=env.namespace, name=notifications.NAME): notifications.service(env.namespace).pods,
    }
    database_url = (
        SecretRef(namespace=env.namespace, name="postgres-session-dispatcher").key("uri").env_value(chart, "database")
    )
    runner_gateway_workload.RunnerGateway(
        chart,
        "runner-gateway",
        env,
        dispatcher_callers=callers,
        gateway_callers=callers,
        wake_callers=frozenset(callers),
        database_url=database_url,
        sandbox_controller=(
            ServiceAccountRef(namespace=env.namespace, name=sandbox_controller.NAME),
            sandbox_controller.pods(env.namespace),
        ),
    ).add_platform_resources()
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def test_gateway_preserves_services_and_fences_listener_ingress() -> None:
    docs = _gateway_objects()
    root = Chart(Cdk8sTesting.app(), "root-services", disable_resource_name_hashes=True)
    runner_gateway_workload.add_stable_services(root, staging.ENV)
    root_docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(root))
    assert not [doc for doc in docs if doc["kind"] == "Service"]
    deployment = _resource(docs, "Deployment", runner_gateway.NAME)
    pod = deployment["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "runner-gateway")
    assert container["image"].startswith("git.allegedly.works/ducktape-ci/agentplane-runner-gateway:")
    assert pod["serviceAccountName"] == runner_gateway.NAME
    http = session_dispatcher.http_service(staging.ENV.namespace)
    assert {p["containerPort"] for p in container["ports"]} == {8080, http.pod_port}
    settings = _gateway_settings(docs, container)
    namespace = staging.ENV.namespace
    assert settings["listen_address"] == "0.0.0.0:8080"
    assert settings["http_listen_address"] == f"0.0.0.0:{http.pod_port}"
    assert settings["dispatcher_audience"] == session_dispatcher.TOKEN_AUDIENCE
    assert settings["gateway_audience"] == runner_gateway.TOKEN_AUDIENCE
    assert set(settings["dispatcher_callers"]) == {f"{namespace}/{app.NAME}", f"{namespace}/{notifications.NAME}"}
    assert f"{namespace}/{runner_gateway.NAME}" in settings["gateway_callers"]
    assert f"{namespace}/{app.NAME}" in settings["wake_callers"]
    assert "initContainers" not in pod
    assert _resource(root_docs, "Service", runner_gateway.NAME)["spec"]["ports"] == [
        {"name": "grpc", "port": 8080, "protocol": "TCP", "targetPort": 8080},
        {"name": "runner-control", "port": 8082, "protocol": "TCP", "targetPort": 8080},
    ]
    assert _resource(root_docs, "Service", session_dispatcher.NAME)["spec"]["ports"] == [
        {"name": "grpc", "port": 8080, "protocol": "TCP", "targetPort": 8080},
        {"name": "http", "port": http.port.number, "protocol": "TCP", "targetPort": http.pod_port},
    ]
    assert _resource(root_docs, "Service", session_dispatcher.NAME)["spec"]["selector"] == {
        "app.kubernetes.io/name": runner_gateway.NAME
    }
    policy = _resource(docs, "CiliumNetworkPolicy", runner_gateway.NAME.removeprefix("agentplane-"))["spec"]
    ingress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), port["port"])
        for rule in policy["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (app.NAME, "8080") in ingress
    assert (app.NAME, str(http.pod_port)) in ingress
    assert (notifications.NAME, "8080") in ingress
    assert (notifications.NAME, str(http.pod_port)) in ingress
    assert (runner_gateway.NAME, "8080") in ingress
    assert (sandbox_controller.NAME, "8080") in ingress
    assert (sandbox_controller.NAME, "8080") in ingress
    role = _resource(docs, "Role", runner_gateway.NAME.removeprefix("agentplane-"))
    assert {rule["resources"][0] for rule in role["rules"]} == {"pods", "sandboxes", "leases", "endpointslices"}
    assert "patch" in one(rule for rule in role["rules"] if rule["resources"] == ["sandboxes"])["verbs"]


def test_the_settings_file_the_gateway_reads_is_mounted_beside_its_token() -> None:
    docs = _gateway_objects()
    pod = _resource(docs, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "runner-gateway")
    path = PurePosixPath(one(entry["value"] for entry in container["env"] if entry["name"] == Settings.config_file_env))
    mounted = {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    settings_map = _resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")
    assert volumes[mounted[str(path.parent)]]["configMap"]["name"] == settings_map["metadata"]["name"]
    assert path.name in settings_map["data"]
    assert "projected" in volumes[mounted["/var/run/secrets/agentplane-runner-gateway"]]


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_runner_platform_route_fences_name_the_gateway_listener_port(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    """Cilium matches after the Service's translation, so each fence must name the Pods' own port."""
    documents = agentplane_manifests[namespace]
    container = one(_resource(documents, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]["containers"])
    listening = {port["containerPort"] for port in container["ports"] if port["name"] == "grpc"}

    def ports(rules: list[dict[str, Any]], peers: str) -> set[int]:
        return {
            int(port["port"])
            for rule in rules
            if any(
                peer["matchLabels"].get("app.kubernetes.io/name") == runner_gateway.NAME for peer in rule.get(peers, [])
            )
            for target in rule["toPorts"]
            for port in target["ports"]
        }

    control = _resource(documents, "CiliumNetworkPolicy", "platform-route-control")["spec"]
    clients = _resource(documents, "CiliumNetworkPolicy", "platform-route-clients")["spec"]
    admitted = {
        int(port["port"]) for rule in control["ingress"] for target in rule["toPorts"] for port in target["ports"]
    }
    assert admitted == listening
    assert ports(clients["egress"], "toEndpoints") == listening


def test_migration_job_is_force_recreated_when_the_pinned_image_changes() -> None:
    chart = Chart(Cdk8sTesting.app(), "migration", disable_resource_name_hashes=True)
    database_url = (
        SecretRef(namespace=staging.ENV.namespace, name="postgres-session-dispatcher")
        .key("uri")
        .env_value(chart, "database")
    )
    runner_gateway_workload.QueueMigration(chart, "migration", staging.ENV, database_url=database_url)
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    job = _resource(docs, "Job", runner_gateway_workload.MIGRATION_JOB)
    # A Job's pod template is immutable; Flux recreates it only on this annotation.
    assert job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"] == "enabled"
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["containers"][0].get("args", []) == []
    assert {item["name"]: item["valueFrom"] for item in pod["containers"][0]["env"]} == {
        "DATABASE_URL": {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    }


if __name__ == "__main__":
    pytest_bazel.main()
