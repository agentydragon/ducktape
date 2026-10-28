"""Invariants over the generated main-proxy config."""

import json
from typing import Any

import pytest_bazel
from litellm.integrations.otel.model.baggage import DEFAULT_BAGGAGE_METADATA_KEYS, promoted_baggage
from litellm.integrations.otel.model.config import OpenTelemetryV2Config
from litellm.integrations.otel.model.metadata import RequestIdentity
from litellm.litellm_core_utils.litellm_logging import (
    StandardLoggingPayloadSetup,
    create_dummy_standard_logging_payload,
)
from litellm.proxy.litellm_pre_call_utils import LiteLLMProxyRequestSetup

from cluster.cdk8s.litellm.config import main_proxy_config, model_entry
from cluster.cdk8s.litellm.proxy import proxy_specs
from cluster.cdk8s.litellm.upstreams import RUGGED_LOCAL_LLM_API_BASE, RUGGED_LOCAL_LLM_KEY_ENV
from model_catalog.catalog import (
    RUGGED_LOCAL_CANDIDATE_ROUTES,
    RUGGED_LOCAL_EXPERIMENTAL_ROUTES,
    RUGGED_LOCAL_GEMMA4_E2B_LONG_CONTEXT_EXPERIMENT,
    RUGGED_LOCAL_QWEN3_4B,
    TANA_ROUTES,
)


def test_tana_routes_register_the_in_process_provider() -> None:
    config = main_proxy_config()
    # Exposure is parked; keep the in-process adapter renderable for re-enablement.
    assert not any(entry["model_name"].startswith("tana/") for entry in config["model_list"])
    tana_entries = [model_entry(route) for route in TANA_ROUTES]
    assert all(entry["model_name"].startswith("tana/oai-chat/") for entry in tana_entries)
    assert all(entry["model_info"]["mode"] == "chat" for entry in tana_entries)
    assert all(entry["litellm_params"]["custom_llm_provider"] == "tana" for entry in tana_entries)
    assert any(item["provider"] == "tana" for item in config["litellm_settings"]["custom_provider_map"])


def test_rugged_routes_register_and_inject_the_bearer_key() -> None:
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
    [spec] = proxy_specs()
    assert RUGGED_LOCAL_LLM_KEY_ENV in {entry.name for entry in spec.env}


def test_long_context_route_has_explicit_litellm_timeouts() -> None:
    long_context = model_entry(RUGGED_LOCAL_GEMMA4_E2B_LONG_CONTEXT_EXPERIMENT)["litellm_params"]
    standard = model_entry(RUGGED_LOCAL_QWEN3_4B)["litellm_params"]

    assert long_context["timeout"] == 2_400
    assert long_context["stream_timeout"] == 2_400
    assert "timeout" not in standard
    assert "stream_timeout" not in standard


def test_langfuse_otel_v2_uses_otlp_when_secret_exists_and_console_without_it() -> None:
    [spec] = proxy_specs()
    env = {entry.name: getattr(entry, "value", None) for entry in spec.env}
    config = main_proxy_config()

    assert env["LITELLM_OTEL_V2"] == "true"
    assert "OTEL_EXPORTER" not in env
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in env
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "span_only"
    assert "OTEL_EXPORTER_OTLP_HEADERS" in env
    secret_env = {entry.name: entry for entry in spec.env if getattr(entry, "optional", False)}
    assert getattr(secret_env["OTEL_EXPORTER_OTLP_ENDPOINT"], "optional", False)
    assert getattr(secret_env["OTEL_EXPORTER_OTLP_HEADERS"], "optional", False)
    assert config["litellm_settings"]["callbacks"] == ["otel", "prometheus"]
    assert config["callback_settings"]["otel"]["mapper_names"] == ["langfuse"]


def test_agentplane_header_metadata_is_promoted_to_otel_baggage() -> None:
    config = main_proxy_config()
    otel_config = OpenTelemetryV2Config.model_validate(config["callback_settings"]["otel"])
    assert set(DEFAULT_BAGGAGE_METADATA_KEYS) <= set(otel_config.baggage_metadata_keys)

    agentplane_metadata = {
        "agentplane.namespace": "agentplane-test",
        "agentplane.service_account": "agentplane-test-runtime",
        "agentplane.pod_name": "agentplane-test-7f9c8",
        "agentplane.pod_uid": "00000000-0000-4000-8000-000000000001",
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
        "litellm.metadata.spend_logs_metadata.agentplane.pod_name": "agentplane-test-7f9c8",
        "litellm.metadata.spend_logs_metadata.agentplane.pod_uid": "00000000-0000-4000-8000-000000000001",
    }
    assert {key: promoted.get(key) for key in expected_baggage} == expected_baggage
    assert "litellm.metadata.spend_logs_metadata.agentplane.debug" not in promoted
    assert "litellm.metadata.spend_logs_metadata.operator_debug" not in promoted


if __name__ == "__main__":
    pytest_bazel.main()
