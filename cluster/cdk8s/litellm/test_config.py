"""Invariants over the generated main-proxy config."""

import pytest_bazel

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


if __name__ == "__main__":
    pytest_bazel.main()
