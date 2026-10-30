"""The Gateway keeps one public Service and uses mTLS client connections to runner Pods."""

from pathlib import PurePosixPath
from typing import Any, cast

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.runner_gateway.settings import Settings
from cluster.cdk8s.agentplane import (
    actions,
    app,
    chart as agentplane_chart,
    notifications,
    runner_gateway,
    runner_gateway_workload,
    runner_sandbox,
    sandbox_controller,
    session_dispatcher,
    staging,
)
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


def _gateway_objects(app: App) -> list[dict[str, Any]]:
    chart = runner_gateway_workload.serving_chart(app, env=staging.ENV)
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def test_environment_provisions_cert_manager_runner_issuer_and_gateway_identity(
    staging_manifests: list[dict[str, Any]],
) -> None:
    issuer_ca = _resource(staging_manifests, "Certificate", agentplane_chart.RUNNER_CA_SECRET)
    assert issuer_ca["spec"]["isCA"] is True
    assert issuer_ca["spec"]["issuerRef"]["kind"] == "ClusterIssuer"
    assert issuer_ca["spec"]["privateKey"]["rotationPolicy"] == "Always"

    issuer = _resource(staging_manifests, "Issuer", runner_gateway.RUNNER_CERTIFICATE_ISSUER)
    assert issuer["spec"]["ca"]["secretName"] == agentplane_chart.RUNNER_CA_SECRET

    gateway_certificate = _resource(staging_manifests, "Certificate", agentplane_chart.RUNNER_GATEWAY_CERTIFICATE)
    spec = gateway_certificate["spec"]
    assert spec["secretName"] == runner_gateway.GATEWAY_TLS_SECRET
    assert spec["issuerRef"] == {"kind": "Issuer", "name": runner_gateway.RUNNER_CERTIFICATE_ISSUER}
    assert spec["uris"] == ["spiffe://agentplane/runner-gateway"]
    assert set(spec["usages"]) == {"digital signature", "client auth"}
    assert spec["privateKey"]["rotationPolicy"] == "Always"


def test_gateway_preserves_services_and_fences_listener_ingress(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    root = Chart(scratch_app, "root-services", disable_resource_name_hashes=True)
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
    assert settings["listen_address"] == "0.0.0.0:8080"
    assert settings["http_listen_address"] == f"0.0.0.0:{http.pod_port}"
    assert settings["dispatcher_audience"] == session_dispatcher.TOKEN_AUDIENCE
    assert settings["gateway_audience"] == runner_gateway.TOKEN_AUDIENCE
    assert settings["runner_security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.crt",
            "key_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.key",
            "ca_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/ca.crt",
        },
    }
    assert set(settings["dispatcher_callers"]) == {
        f"{staging.ENV.namespace}/{app.NAME}",
        f"{staging.ENV.namespace}/{notifications.NAME}",
        f"{staging.ENV.namespace}/{actions.NAME}",
    }
    assert f"{staging.ENV.namespace}/{runner_gateway.NAME}" in settings["gateway_callers"]
    assert f"{staging.ENV.namespace}/{app.NAME}" in settings["wake_callers"]
    migration = one(pod["initContainers"])
    assert migration["name"] == "migrate"
    assert migration["image"] == "git.allegedly.works/ducktape-ci/agentplane-runner-gateway-queue-migrate:unset"
    assert {entry["name"]: entry["valueFrom"] for entry in migration["env"]} == {
        "DATABASE_URL": {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    }
    assert _resource(root_docs, "Service", runner_gateway.NAME)["spec"]["ports"] == [
        {"name": "grpc", "port": 8080, "protocol": "TCP", "targetPort": 8080}
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
    runner_label = runner_sandbox.pods(staging.ENV.namespace).selector["app.kubernetes.io/name"]
    assert (runner_label, "8090") not in ingress
    runner_egress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), str(port["port"]))
        for rule in policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (runner_label, str(runner_sandbox.RUNNER_SERVICE_PORT)) in runner_egress
    role = _resource(docs, "Role", runner_gateway.NAME.removeprefix("agentplane-"))
    assert {resource for rule in role["rules"] for resource in rule["resources"]} >= {
        "pods",
        "sandboxes",
        "leases",
        "endpointslices",
        "virtualmachines",
        "virtualmachineinstances",
    }
    assert "patch" in one(rule for rule in role["rules"] if rule["resources"] == ["sandboxes"])["verbs"]


def test_the_settings_file_the_gateway_reads_is_mounted_beside_its_token(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    pod = _resource(docs, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "runner-gateway")
    path = PurePosixPath(one(entry["value"] for entry in container["env"] if entry["name"] == Settings.config_file_env))
    mounted = {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    settings_map = _resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")
    tls_volume = volumes[mounted[runner_gateway.GATEWAY_TLS_MOUNT_PATH]]["projected"]
    assert tls_volume["defaultMode"] == 0o440
    secret_source = one(source["secret"] for source in tls_volume["sources"] if "secret" in source)
    assert secret_source["name"] == runner_gateway.GATEWAY_TLS_SECRET
    assert {item["path"] for item in secret_source["items"]} == {"tls.crt", "tls.key"}
    trust_source = one(source["configMap"] for source in tls_volume["sources"] if "configMap" in source)
    assert trust_source["name"] == "cluster-internal-ca-bundle"
    assert trust_source["items"] == [{"key": "ca-certificates.crt", "path": "ca.crt"}]
    assert volumes[mounted[str(path.parent)]]["configMap"]["name"] == settings_map["metadata"]["name"]
    assert path.name in settings_map["data"]
    assert "projected" in volumes[mounted["/var/run/secrets/agentplane-runner-gateway"]]


def test_runner_mtls_is_direct_and_outside_platform_routes(staging_manifests: list[dict[str, Any]]) -> None:
    routes = _resource(staging_manifests, "CiliumNetworkPolicy", "platform-route-clients")["spec"]
    routed_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in routes["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert runner_gateway.NAME not in routed_services

    gateway = _resource(staging_manifests, "CiliumNetworkPolicy", "runner-gateway")["spec"]
    assert all(
        port["port"] != "8090"
        for rule in gateway["ingress"]
        for target in rule.get("toPorts", [])
        for port in target["ports"]
    )
    runner_label = runner_sandbox.pods(staging.ENV.namespace).selector["app.kubernetes.io/name"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == runner_label
        and any(
            port["port"] == str(runner_sandbox.RUNNER_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in gateway["egress"]
        for endpoint in rule.get("toEndpoints", [])
    )
    runner_listener = _resource(staging_manifests, "CiliumNetworkPolicy", "runner-listener")["spec"]
    gateway_label = runner_gateway.service(staging.ENV.namespace).pods.selector["app.kubernetes.io/name"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == gateway_label
        and any(
            port["port"] == str(runner_sandbox.RUNNER_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in runner_listener["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


if __name__ == "__main__":
    pytest_bazel.main()
