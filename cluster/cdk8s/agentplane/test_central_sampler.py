"""The optional Central Sampler's rendered workload and service-to-service grants."""

from __future__ import annotations

import json
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import central_sampler, history_service, llm_ingress, runner_gateway, staging


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


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
    assert _resource(staging_manifests, "Database", "central-sampler")["spec"]["databaseReclaimPolicy"] == "retain"
    deployment = _resource(staging_manifests, "Deployment", central_sampler.NAME)
    pod = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert pod["serviceAccountName"] == central_sampler.NAME

    container = one(pod["containers"])
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

    token_volume = one(volume for volume in pod["volumes"] if volume["name"] == "caller-tokens")
    token_paths = {
        token["serviceAccountToken"]["path"]: token["serviceAccountToken"]["audience"]
        for token in token_volume["projected"]["sources"]
    }
    assert token_paths == {
        "llm": WORKLOAD_TOKEN_AUDIENCE,
        "gateway": runner_gateway.TOKEN_AUDIENCE,
        "history": history_service.RUNNER_INGRESS_AUDIENCE,
    }
    config = json.loads(_resource(staging_manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"])
    assert "database_url" not in config
    assert config["audience"] == central_sampler.TOKEN_AUDIENCE
    props = staging.ENV.central_sampler
    assert props is not None
    assert config["llm_proxy_url"] == props.llm_proxy_url
    assert config["rpc_timeout_seconds"] == props.rpc_timeout_seconds
    assert config["executor_initialize_timeout_seconds"] == props.executor_initialize_timeout_seconds
    assert config["credential_file"] == "/var/run/secrets/central-sampler/llm"
    assert "credential" not in config

    gateway = yaml.safe_load(
        _resource(staging_manifests, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"]
    )
    target = gateway["central_sampler"]
    assert target == {
        "target": central_sampler.target(namespace),
        "token_file": "/var/run/secrets/agentplane-runner-gateway/central-sampler",
    }
    assert gateway["executor_callers"] == [f"{namespace}/{central_sampler.NAME}"]

    history = yaml.safe_load(
        _resource(staging_manifests, "ConfigMap", f"{history_service.NAME}-config")["data"]["config.yaml"]
    )
    assert history["ingress"]["sampler_accounts"] == [{"namespace": namespace, "name": central_sampler.NAME}]
    llm = yaml.safe_load(_resource(staging_manifests, "ConfigMap", "llm-ingress-settings")["data"]["settings.yaml"])
    assert llm["central_sampler_accounts"] == [f"{namespace}/{central_sampler.NAME}"]

    assert _admits(
        _resource(staging_manifests, "CiliumNetworkPolicy", central_sampler.NAME)["spec"],
        source=runner_gateway.NAME,
        port=central_sampler.PORT,
    )
    assert _admits(
        _resource(staging_manifests, "CiliumNetworkPolicy", runner_gateway.NAME)["spec"],
        source=central_sampler.NAME,
        port=runner_gateway.service(namespace).pod_port,
    )
    assert _admits(
        _resource(staging_manifests, "CiliumNetworkPolicy", history_service.NAME)["spec"],
        source=central_sampler.NAME,
        port=history_service.ingress_service(namespace).pod_port,
    )
    assert _admits(
        _resource(staging_manifests, "CiliumNetworkPolicy", "llm-ingress")["spec"],
        source=central_sampler.NAME,
        port=llm_ingress.service(namespace).pod_port,
    )


if __name__ == "__main__":
    pytest_bazel.main()
