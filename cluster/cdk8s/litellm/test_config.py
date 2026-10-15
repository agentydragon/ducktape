"""Invariants over the generated main-proxy config."""

import pytest_bazel

from cluster.cdk8s.litellm.config import main_proxy_config, model_entry
from cluster.cdk8s.litellm.proxy import proxy_specs
from model_catalog.catalog import TANA_ROUTES


def test_tana_routes_register_the_in_process_provider() -> None:
    config = main_proxy_config()
    # Exposure is parked; keep the in-process adapter renderable for re-enablement.
    assert not any(entry["model_name"].startswith("tana/") for entry in config["model_list"])
    tana_entries = [model_entry(route) for route in TANA_ROUTES]
    assert all(entry["litellm_params"]["custom_llm_provider"] == "tana" for entry in tana_entries)
    assert any(item["provider"] == "tana" for item in config["litellm_settings"]["custom_provider_map"])


def test_paused_rugged_routes_are_not_registered_or_injected_into_litellm() -> None:
    config = main_proxy_config()
    assert not any(entry["model_name"].startswith("rugged-local/") for entry in config["model_list"])
    [spec] = proxy_specs()
    assert "RUGGED_LOCAL_LLM_KEY" not in {entry.name for entry in spec.env}


if __name__ == "__main__":
    pytest_bazel.main()
