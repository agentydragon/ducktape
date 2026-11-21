"""Invariants over the generated main-proxy config."""

import json
import re
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart
from litellm.integrations.otel.model.baggage import DEFAULT_BAGGAGE_METADATA_KEYS, promoted_baggage
from litellm.integrations.otel.model.config import CaptureMessageContent, ExporterOwner, OpenTelemetryV2Config
from litellm.integrations.otel.model.metadata import RequestIdentity
from litellm.litellm_core_utils.litellm_logging import (
    StandardLoggingPayloadSetup,
    create_dummy_standard_logging_payload,
)
from litellm.proxy.litellm_pre_call_utils import LiteLLMProxyRequestSetup

from cluster.cdk8s.litellm.config import main_proxy_config, model_entry
from cluster.cdk8s.litellm.proxy import LiteLLMProxy
from cluster.cdk8s.litellm.secrets import LANGFUSE_EXPORTERS_TEMPLATE
from cluster.cdk8s.litellm.upstreams import RUGGED_LOCAL_LLM_API_BASE, RUGGED_LOCAL_LLM_KEY_ENV
from cluster.cdk8s.testing.manifests import find_resource
from model_catalog.catalog import (
    RUGGED_LOCAL_CANDIDATE_ROUTES,
    RUGGED_LOCAL_EXPERIMENTAL_ROUTES,
    RUGGED_LOCAL_OPENAI,
    TABBYAPI_QWEN_ABLITERATED,
    TABBYAPI_QWEN_STOCK,
    TANA_ROUTES,
    Model,
    Route,
)


@pytest.fixture(scope="module")
def deployment_env() -> dict[str, dict[str, Any]]:
    app = App()
    chart = Chart(app, "litellm", disable_resource_name_hashes=True)
    LiteLLMProxy(chart, "proxy")
    deployment = find_resource(yaml.safe_load_all(app.synth_yaml()), "Deployment", "litellm", namespace="litellm")
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    return {entry["name"]: entry for entry in container["env"]}


def test_tana_routes_project_to_the_in_process_provider() -> None:
    config = main_proxy_config()
    tana_entries = [model_entry(route) for route in TANA_ROUTES]
    assert all(entry["model_name"].startswith("tana/oai-chat/") for entry in tana_entries)
    assert all(entry["model_info"]["mode"] == "chat" for entry in tana_entries)
    assert all(entry["litellm_params"]["custom_llm_provider"] == "tana" for entry in tana_entries)
    assert any(item["provider"] == "tana" for item in config["litellm_settings"]["custom_provider_map"])


def test_rugged_routes_register_and_inject_the_bearer_key(deployment_env: dict[str, dict[str, Any]]) -> None:
    config = main_proxy_config()
    rugged_routes = (*RUGGED_LOCAL_CANDIDATE_ROUTES, *RUGGED_LOCAL_EXPERIMENTAL_ROUTES)
    configured_routes = {
        entry["model_name"]: entry for entry in config["model_list"] if entry["model_name"].startswith("rugged-local/")
    }
    assert set(configured_routes) == {route.id for route in rugged_routes}
    for route in rugged_routes:
        params = configured_routes[route.id]["litellm_params"]
        assert params["model"] == f"openai/{route.upstream_model}"
        assert params["api_base"] == RUGGED_LOCAL_LLM_API_BASE
        assert params["api_key"] == f"os.environ/{RUGGED_LOCAL_LLM_KEY_ENV}"
    assert RUGGED_LOCAL_LLM_KEY_ENV in deployment_env


def test_stock_qwen_routes_to_tabbyapi() -> None:
    entries = {entry["model_name"]: entry for entry in main_proxy_config()["model_list"]}
    assert not any("iq4xs" in name for name in entries)
    params = entries[TABBYAPI_QWEN_STOCK.id]["litellm_params"]
    assert params["model"] == "hosted_vllm/qwen38-exl3-268"
    assert params["api_base"] == "http://tabbyapi-model-switching.ollama.svc:5000/v1"
    assert params["allowed_openai_params"] == ["reasoning_effort"]
    assert params["use_chat_completions_api"] is True


def test_abliterated_qwen_routes_to_its_own_checkpoint() -> None:
    [entry] = [
        entry for entry in main_proxy_config()["model_list"] if entry["model_name"] == TABBYAPI_QWEN_ABLITERATED.id
    ]
    assert entry["model_info"] == {"mode": "chat", "supports_function_calling": True}
    assert entry["litellm_params"] == {
        "model": "hosted_vllm/qwen38-exl3-250-abliterated",
        "api_base": "http://tabbyapi-model-switching.ollama.svc:5000/v1",
        "api_key": "tabbyapi",
        "allowed_openai_params": ["reasoning_effort"],
        "use_chat_completions_api": True,
    }


def test_route_timeouts_are_projected_independently() -> None:
    timed_route = Route(Model("synthetic-timeout-route"), RUGGED_LOCAL_OPENAI, timeout=37, stream_timeout=83)
    untimed_route = Route(Model("synthetic-untimed-route"), RUGGED_LOCAL_OPENAI)
    timed = model_entry(timed_route)["litellm_params"]
    untimed = model_entry(untimed_route)["litellm_params"]

    assert timed["timeout"] == 37
    assert timed["stream_timeout"] == 83
    assert "timeout" not in untimed
    assert "stream_timeout" not in untimed


def test_langfuse_exporter_is_model_call_scoped_and_console_without_secret(
    deployment_env: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = main_proxy_config()
    assert deployment_env["EXPORTERS"]["valueFrom"]["secretKeyRef"]["optional"] is True
    assert config["litellm_settings"]["callbacks"] == ["otel", "prometheus"]
    assert config["callback_settings"]["otel"]["mapper_names"] == ["langfuse"]

    # LiteLLM's own settings loader reads the Deployment's literal env; ESO renders the Go
    # template in EXPORTERS, so any credential string stands in for it here.
    for name, entry in deployment_env.items():
        if "value" in entry:
            monkeypatch.setenv(name, entry["value"])
    without_secret = OpenTelemetryV2Config()
    assert [exporter.kind for exporter in without_secret.exporters] == ["console"]

    monkeypatch.setenv("EXPORTERS", re.sub(r"\{\{.*?\}\}", "ZmFrZTpmYWtl", LANGFUSE_EXPORTERS_TEMPLATE))
    otel = OpenTelemetryV2Config()
    [exporter] = otel.exporters
    assert exporter.kind == "otlp_http"
    assert exporter.owner is ExporterOwner.LANGFUSE_OTEL
    assert otel.langfuse_span_scope == "llm_only"
    assert otel.capture_message_content == CaptureMessageContent.SPAN_ONLY


def test_agentplane_header_metadata_is_promoted_to_otel_baggage() -> None:
    config = main_proxy_config()
    otel_config = OpenTelemetryV2Config.model_validate(config["callback_settings"]["otel"])
    assert set(DEFAULT_BAGGAGE_METADATA_KEYS) <= set(otel_config.baggage_metadata_keys)

    agentplane_metadata = {
        "agentplane.namespace": "agentplane-test",
        "agentplane.service_account": "agentplane-test-runtime",
        "agentplane.identity": "test-agent-identity",
        "agentplane.identity_uid": "00000000-0000-4000-8000-000000000003",
        "agentplane.pod_name": "agentplane-test-7f9c8",
        "agentplane.pod_uid": "00000000-0000-4000-8000-000000000001",
        "agentplane.session_id": "00000000-0000-4000-8000-000000000002",
        "agentplane.debug": "unlisted-agentplane-value",
        "operator_debug": "unlisted-value",
    }
    request_data: dict[str, Any] = {
        "metadata": {
            "user_api_key_org_id": "org-test",
            "user_api_key_user_id": "user-test",
            "user_api_key_alias": "key-test",
            "user_api_key_end_user_id": "end-user-test",
            "requester_ip_address": "192.0.2.42",
        }
    }
    LiteLLMProxyRequestSetup.add_litellm_metadata_from_request_headers(
        headers={"x-litellm-spend-logs-metadata": json.dumps(agentplane_metadata)},
        data=request_data,
        _metadata_variable_name="metadata",
    )
    standard_metadata = StandardLoggingPayloadSetup.get_standard_logging_metadata(metadata=request_data["metadata"])
    payload = create_dummy_standard_logging_payload()
    payload["metadata"] = standard_metadata
    identity = RequestIdentity.from_payload(payload)
    promoted = promoted_baggage(
        identity,
        request_model=None,
        promoted_keys=tuple(otel_config.baggage_promoted_keys),
        metadata_keys=tuple(otel_config.baggage_metadata_keys),
        team_metadata_keys=tuple(otel_config.baggage_team_metadata_keys),
    )

    expected_baggage = {
        "litellm.metadata.user_api_key_org_id": "org-test",
        "litellm.metadata.user_api_key_user_id": "user-test",
        "litellm.metadata.user_api_key_alias": "key-test",
        "litellm.metadata.user_api_key_end_user_id": "end-user-test",
        "litellm.metadata.requester_ip_address": "192.0.2.42",
        "litellm.metadata.spend_logs_metadata.agentplane.namespace": "agentplane-test",
        "litellm.metadata.spend_logs_metadata.agentplane.service_account": "agentplane-test-runtime",
        "litellm.metadata.spend_logs_metadata.agentplane.identity": "test-agent-identity",
        "litellm.metadata.spend_logs_metadata.agentplane.identity_uid": "00000000-0000-4000-8000-000000000003",
        "litellm.metadata.spend_logs_metadata.agentplane.pod_name": "agentplane-test-7f9c8",
        "litellm.metadata.spend_logs_metadata.agentplane.pod_uid": "00000000-0000-4000-8000-000000000001",
        "litellm.metadata.spend_logs_metadata.agentplane.session_id": "00000000-0000-4000-8000-000000000002",
    }
    assert {key: promoted.get(key) for key in expected_baggage} == expected_baggage
    assert "litellm.metadata.spend_logs_metadata.agentplane.debug" not in promoted
    assert "litellm.metadata.spend_logs_metadata.operator_debug" not in promoted


if __name__ == "__main__":
    pytest_bazel.main()
