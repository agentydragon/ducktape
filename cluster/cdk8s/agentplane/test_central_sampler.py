"""The optional Central Sampler's cross-service contracts and policies."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest_bazel
from cdk8s import Chart
from cdk8s_plus_34 import ConfigMap, EnvValue
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
    central_sampler_workload,
    history_service,
    llm_ingress,
    policy_controller,
    runner_gateway,
    sandbox_controller,
    staging,
    workload_tls,
)
from cluster.cdk8s.agentplane.model_selections import STAGING_APP_MODELS, chat_completions_id
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource
from model_catalog.catalog import Provider


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
    container = one(pod["containers"])
    assert container["startupProbe"]["grpc"]["port"] == central_sampler.HEALTH_PORT
    serving_certificate = find_resource(staging_manifests, "Certificate", f"{central_sampler.NAME}-workload")
    assert serving_certificate["spec"]["uris"] == [CENTRAL_SAMPLER_URI]
    env = {entry["name"]: entry for entry in container["env"]}
    database = env["AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]
    assert database == {"key": "uri", "name": "postgres-central-sampler"}

    config = json.loads(
        find_resource(staging_manifests, "ConfigMap", "central-sampler-settings")["data"]["settings.json"]
    )
    assert "database_url" not in config
    assert config["service_callers"] == [GATEWAY_SESSIONS_URI]
    assert "admin_service_callers" not in config
    assert config["health_listen"] == f"0.0.0.0:{central_sampler.HEALTH_PORT}"
    assert config["listen_tls"] == {
        "listen": f"0.0.0.0:{central_sampler.PORT}",
        "cert_file": workload_tls.CERT_FILE,
        "key_file": workload_tls.KEY_FILE,
        "client_ca_file": workload_tls.TRUST_BUNDLE_FILE,
    }
    assert config["platform_instructions"]
    llm_connection = config["connections"]["llm-ingress"]
    llm_service = llm_ingress.service(namespace)
    assert llm_connection["base_url"] == f"https://{llm_service.authority}"
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

    gateway = config_map_yaml(staging_manifests, f"{runner_gateway.NAME}-settings", "settings.yaml")
    target = gateway["central_sampler"]
    assert target["target"] == central_sampler.service(namespace).authority
    assert "token_file" not in target
    assert target["tls"]["server_uri"] == CENTRAL_SAMPLER_URI
    assert target["tls"]["client_uri"] == GATEWAY_SESSIONS_URI
    for field in ("cert_file", "key_file", "ca_file"):
        assert target["tls"][field] == gateway["gateway_tls"][field]
    controller = config_map_yaml(staging_manifests, f"{sandbox_controller.NAME}-settings", "config.yaml")
    assert controller["environment_service"]["runtime_proxy"]["executor_callers"] == serving_certificate["spec"]["uris"]

    app_config = config_map_yaml(staging_manifests, "app-config", "config.yaml")
    assert app_config["policy_service_target"] == policy_controller.tls_service(namespace).authority
    assert app_config["policy_service_tls"]["server_uri"] == POLICY_CONTROLLER_URI

    history = config_map_yaml(staging_manifests, f"{history_service.NAME}-config", "config.yaml")
    assert history["policy_namespace"] == namespace
    assert serving_certificate["spec"]["uris"] == [history["ingress"]["sampler_service_actor"]]
    llm = config_map_yaml(staging_manifests, "llm-ingress-settings", "settings.yaml")
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


def test_central_sampler_turn_timeout_option_is_configured(scratch_chart: Chart) -> None:
    sampler = staging.ENV.central_sampler
    assert sampler is not None
    env = replace(staging.ENV, central_sampler=replace(sampler, turn_timeout_seconds=123))
    central_sampler_workload.CentralSampler(
        scratch_chart, "sampler", env, database_url=EnvValue.from_value("postgresql://test.invalid/sampler")
    )
    settings_map = one(
        construct
        for construct in scratch_chart.node.find_all()
        if isinstance(construct, ConfigMap) and construct.name == "central-sampler-settings"
    )
    config = json.loads(settings_map.data["settings.json"])
    assert config["turn_timeout_seconds"] == 123


def test_staging_picker_and_presets_offer_models_the_harness_can_run() -> None:
    assert staging.ENV.app is not None
    assert staging.ENV.central_sampler is not None
    app = staging.ENV.app.config
    sampler = staging.ENV.central_sampler.models
    harnesses = app.models.harnesses
    assert set(harnesses[Harness.CENTRAL_SAMPLER]) == set(sampler)
    assert set(harnesses[Harness.CLAUDE]) <= set(sampler)
    assert set(harnesses[Harness.CODEX]) < set(sampler)
    for model_id, model in sampler.items():
        if model.api == "chat_completions":
            assert model_id not in harnesses[Harness.CLAUDE]
            if model_id != model.provider_model:
                assert model_id not in harnesses[Harness.CODEX]
    options = {option.model: option for option in app.models.models}
    for name, preset in app.thread_presets.items():
        assert preset.model in harnesses[preset.harness], name
        if preset.reasoning_effort is not None:
            assert preset.reasoning_effort in options[preset.model].reasoning_efforts, name
    for option in app.models.models:
        if option.model in sampler:
            assert set(option.reasoning_efforts) <= sampler[option.model].reasoning_efforts


def test_staging_sampler_routes_use_the_provider_protocol() -> None:
    assert staging.ENV.central_sampler is not None
    models = staging.ENV.central_sampler.models
    for route in STAGING_APP_MODELS.central_sampler_chat:
        model = models[chat_completions_id(route, STAGING_APP_MODELS.central_sampler)]
        assert (model.api, model.provider_model, model.connection) == ("chat_completions", route.id, "llm-ingress")
    for route in STAGING_APP_MODELS.central_sampler:
        model = models[route.id]
        if route.upstream.provider == Provider.ANTHROPIC_API:
            assert (model.api, model.provider_model, model.connection) == (
                "anthropic_messages",
                route.model.id,
                "anthropic-api",
            )
        else:
            assert (model.api, model.provider_model, model.connection) == ("responses", route.id, "llm-ingress")
            assert model.responses is not None
            if route.upstream.provider == Provider.CHATGPT:
                assert (model.responses.dialect, model.exec_encoding) == ("lite", "freeform")
            else:
                assert (model.responses.dialect, model.exec_encoding) == ("standard", "json")


if __name__ == "__main__":
    pytest_bazel.main()
