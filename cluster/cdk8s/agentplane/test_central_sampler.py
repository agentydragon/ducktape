"""The optional Central Sampler's rendered workload and service-to-service grants."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting  # alias avoids pytest collecting Testing as a test class
from more_itertools import one

from agentplane.env.runtime.harness import Harness
from agentplane.net.peertls.config import (
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    HISTORY_SERVICE_URI,
    LLM_INGRESS_URI,
    POLICY_CONTROLLER_URI,
    SANDBOX_CONTROLLER_URI,
)
from cluster.cdk8s.agentplane import (
    central_sampler,
    history_service,
    llm_ingress,
    policy_controller,
    runner_gateway,
    sandbox_controller,
    staging,
    workload_tls,
)
from cluster.cdk8s.testing.manifests import find_resource


def _admits(policy: dict[str, Any], *, source: str, port: int) -> bool:
    return any(
        any(
            peer.get("matchLabels", {}).get("app.kubernetes.io/name") == source
            for peer in rule.get("fromEndpoints", [])
        )
        and any(
            str(port) == allowed["port"]
            for port_group in rule.get("toPorts", [])
            for allowed in port_group.get("ports", [])
        )
        for rule in policy.get("ingress", [])
    )


def test_central_sampler_workload_and_cross_service_grants(staging_manifests: list[dict[str, Any]]) -> None:
    namespace = staging.ENV.namespace
    assert find_resource(staging_manifests, "Database", "central-sampler")["spec"]["databaseReclaimPolicy"] == "retain"
    deployment = find_resource(staging_manifests, "Deployment", central_sampler.NAME)
    assert set(deployment["metadata"]["annotations"]["configmap.reloader.stakater.com/reload"].split(",")) == {
        "central-sampler-settings",
        "public-web-pki",
    }
    pod = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 2
    assert deployment["spec"]["minReadySeconds"] == 5
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
    }
    assert find_resource(staging_manifests, "Deployment", "sessions")["spec"]["strategy"]["type"] == "RollingUpdate"
    assert find_resource(staging_manifests, "PodDisruptionBudget", central_sampler.NAME)["spec"]["minAvailable"] == 1
    assert pod["serviceAccountName"] == central_sampler.NAME
    assert not pod.get("initContainers")

    container = one(pod["containers"])
    serving_certificate = find_resource(staging_manifests, "Certificate", f"{central_sampler.NAME}-workload")
    assert serving_certificate["spec"]["uris"] == [CENTRAL_SAMPLER_URI]
    assert any(
        volume.get("secret", {}).get("secretName") == f"{central_sampler.NAME}-workload-tls"
        for volume in pod["volumes"]
    )
    service = find_resource(staging_manifests, "Service", central_sampler.NAME)["spec"]
    assert service["selector"]
    assert all(
        deployment["spec"]["template"]["metadata"]["labels"][key] == value for key, value in service["selector"].items()
    )
    service_ports = {port["name"]: port for port in service["ports"]}
    container_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    assert set(service_ports) == {"grpc-tls"}
    assert service_ports["grpc-tls"]["targetPort"] == container_ports["grpc-tls"] == central_sampler.PORT
    assert container_ports["health"] == central_sampler.HEALTH_PORT
    assert container["startupProbe"]["grpc"]["port"] == central_sampler.HEALTH_PORT
    env = {entry["name"]: entry for entry in container["env"]}
    database = env["AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]
    assert database == {"key": "uri", "name": "postgres-central-sampler"}
    settings_path = env["AGENTPLANE_CENTRAL_SAMPLER_CONFIG"]["value"]
    settings_volume = one(
        volume for volume in pod["volumes"] if volume.get("configMap", {}).get("name") == "central-sampler-settings"
    )
    assert any(
        mount["name"] == settings_volume["name"] and mount["mountPath"] == "/etc/agentplane-central-sampler"
        for mount in container["volumeMounts"]
    )
    assert settings_path == "/etc/agentplane-central-sampler/settings.json"

    logs = one(volume for volume in pod["volumes"] if volume["name"] == "logs")
    assert "emptyDir" in logs
    assert any(
        mount["name"] == "logs" and mount["mountPath"] == "/var/log/central-sampler"
        for mount in container["volumeMounts"]
    )

    config = json.loads(
        find_resource(staging_manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"]
    )
    assert "database_url" not in config
    assert "executor_reconnect_timeout_seconds" not in config
    assert "turn_timeout_seconds" not in config
    assert "audience" not in config
    assert config["service_callers"] == [GATEWAY_SESSIONS_URI]
    assert "admin_service_callers" not in config
    assert "listen" not in config
    assert config["health_listen"] == f"0.0.0.0:{central_sampler.HEALTH_PORT}"
    assert config["listen_tls"] == {
        "listen": f"0.0.0.0:{central_sampler.PORT}",
        "cert_file": workload_tls.CERT_FILE,
        "key_file": workload_tls.KEY_FILE,
        "client_ca_file": workload_tls.TRUST_BUNDLE_FILE,
    }
    platform_instructions = config["platform_instructions"]
    assert platform_instructions
    llm_connection = config["connections"]["llm-ingress"]
    llm_service = llm_ingress.service(namespace)
    assert llm_connection["base_url"] == f"https://{llm_service.fqdn}:{llm_service.port.number}"
    assert llm_connection["auth"] == {"type": "agentplane_ingress"}
    assert "history_token_file" not in config
    assert "gateway_token_file" not in config
    assert config["gateway_target"] == "environments.{namespace}.svc:8082"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    mounts = {PurePosixPath(m["mountPath"]): volumes[m["name"]] for m in container["volumeMounts"]}
    for tls, server_uri in (
        (config["history_tls"], HISTORY_SERVICE_URI),
        (config["gateway_tls"], SANDBOX_CONTROLLER_URI),
        (llm_connection["tls"], LLM_INGRESS_URI),
    ):
        assert tls["server_uri"] == server_uri
        assert tls["client_uri"] == serving_certificate["spec"]["uris"][0]
        assert tls["ca_file"] == config["listen_tls"]["client_ca_file"]
        assert "configMap" in mounts[PurePosixPath(tls["ca_file"]).parent]
        for field in ("cert_file", "key_file"):
            path = PurePosixPath(tls[field])
            assert mounts[path.parent]["secret"]["secretName"] == serving_certificate["spec"]["secretName"]
    anthropic_connection = config["connections"]["anthropic-api"]
    assert anthropic_connection["base_url"] == "https://api.anthropic.com"
    assert anthropic_connection["auth"] == {
        "type": "anthropic_api_key",
        "credential_file": "/var/run/secrets/central-sampler-anthropic/api-key",
    }
    assert all("credential" not in connection["auth"] for connection in config["connections"].values())

    latest_anthropic = {
        route.id: route
        for route in staging.STAGING_APP_MODELS.central_sampler
        if route.upstream.provider.value == "anthropic-api"
    }
    catalog = yaml.safe_load(find_resource(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])[
        "models"
    ]
    assert set(latest_anthropic).issubset(catalog["harnesses"][Harness.CENTRAL_SAMPLER.value])
    assert not set(latest_anthropic) & set(catalog["harnesses"][Harness.CODEX.value])
    assert not set(latest_anthropic) & set(catalog["harnesses"][Harness.CLAUDE.value])
    assert set(latest_anthropic).issubset(config["models"])
    for route_id, route in latest_anthropic.items():
        model = config["models"][route_id]
        assert model["connection"] == "anthropic-api"
        assert model["provider_model"] == route.model.id
        assert model["api"] == "anthropic_messages"
        assert model["exec_encoding"] == "json"

    anthropic_secret_volume = one(
        volume
        for volume in pod["volumes"]
        if volume.get("secret", {}).get("secretName") == "agentplane-staging-anthropic-api-key"
    )
    assert anthropic_secret_volume["secret"]["items"] == [{"key": "api-key", "path": "api-key"}]
    assert any(
        mount["name"] == anthropic_secret_volume["name"]
        and mount["mountPath"] == "/var/run/secrets/central-sampler-anthropic"
        and mount["readOnly"]
        for mount in container["volumeMounts"]
    )

    gateway = yaml.safe_load(
        find_resource(staging_manifests, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"]
    )
    target = gateway["central_sampler"]
    assert target["target"] == central_sampler.target(namespace)
    assert "token_file" not in target
    assert target["tls"]["server_uri"] == CENTRAL_SAMPLER_URI
    assert target["tls"]["client_uri"] == GATEWAY_SESSIONS_URI
    for field in ("cert_file", "key_file", "ca_file"):
        assert target["tls"][field] == gateway["gateway_tls"][field]
    controller = yaml.safe_load(
        find_resource(staging_manifests, "ConfigMap", f"{sandbox_controller.NAME}-settings")["data"]["config.yaml"]
    )
    assert controller["environment_service"]["runtime_proxy"]["executor_callers"] == serving_certificate["spec"]["uris"]

    app_config = yaml.safe_load(find_resource(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    assert app_config["policy_service_target"] == policy_controller.tls_target(namespace)
    assert app_config["policy_service_tls"]["server_uri"] == POLICY_CONTROLLER_URI

    history = yaml.safe_load(
        find_resource(staging_manifests, "ConfigMap", f"{history_service.NAME}-config")["data"]["config.yaml"]
    )
    assert history["policy_namespace"] == namespace
    assert serving_certificate["spec"]["uris"] == [history["ingress"]["sampler_service_actor"]]
    llm = yaml.safe_load(find_resource(staging_manifests, "ConfigMap", "llm-ingress-settings")["data"]["settings.yaml"])
    assert llm["policy_namespace"] == namespace
    assert llm["central_sampler_services"] == [serving_certificate["spec"]["uris"][0]]
    assert llm["central_sampler_identity_callers"] is True

    environments = find_resource(staging_manifests, "CiliumNetworkPolicy", "environments")["spec"]
    assert _admits(environments, source=central_sampler.NAME, port=8082)
    sampler_environments = find_resource(
        staging_manifests, "CiliumNetworkPolicy", "environments-client-central-sampler"
    )["spec"]
    assert sampler_environments["endpointSelector"]["matchLabels"] == {"app.kubernetes.io/name": central_sampler.NAME}
    assert sampler_environments["egress"] == [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "app.kubernetes.io/name": "environments",
                        "k8s:io.kubernetes.pod.namespace": namespace,
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "8082", "protocol": "TCP"}]}],
        }
    ]

    sampler_policy = find_resource(staging_manifests, "CiliumNetworkPolicy", central_sampler.NAME)["spec"]
    assert not _admits(sampler_policy, source=runner_gateway.NAME, port=8080)
    assert _admits(
        find_resource(staging_manifests, "CiliumNetworkPolicy", "sessions-sampler")["spec"],
        source=runner_gateway.NAME,
        port=central_sampler.PORT,
    )
    assert not _admits(
        find_resource(staging_manifests, "CiliumNetworkPolicy", runner_gateway.NAME)["spec"],
        source=central_sampler.NAME,
        port=runner_gateway.sessions_service(namespace).pod_port,
    )
    assert _admits(
        find_resource(staging_manifests, "CiliumNetworkPolicy", history_service.NAME)["spec"],
        source=central_sampler.NAME,
        port=history_service.service(namespace).pod_port,
    )
    assert _admits(
        find_resource(staging_manifests, "CiliumNetworkPolicy", "llm-ingress")["spec"],
        source=central_sampler.NAME,
        port=llm_ingress.service(namespace).pod_port,
    )
    sampler_policy = find_resource(staging_manifests, "CiliumNetworkPolicy", central_sampler.NAME)["spec"]
    anthropic_egress = one(rule for rule in sampler_policy["egress"] if "toFQDNs" in rule)
    assert anthropic_egress["toFQDNs"] == [{"matchName": "api.anthropic.com"}]
    assert anthropic_egress["toPorts"] == [
        {"ports": [{"port": "443", "protocol": "TCP"}], "serverNames": ["api.anthropic.com"]}
    ]


def test_central_sampler_turn_timeout_option_is_rendered(scratch_app_factory: Callable[[], App]) -> None:
    sampler = staging.ENV.central_sampler
    assert sampler is not None
    env = replace(staging.ENV, central_sampler=replace(sampler, turn_timeout_seconds=123))
    manifests = Cdk8sTesting.synth(staging.chart(scratch_app_factory(), env))
    config = json.loads(find_resource(manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"])
    assert config["turn_timeout_seconds"] == 123


if __name__ == "__main__":
    pytest_bazel.main()
