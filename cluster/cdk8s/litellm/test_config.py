"""Invariants over the generated main-proxy config."""

import pytest_bazel

from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.model_rosters import ApiShape


# litellm_config.py derives each entry's shape from shape_for(upstream_prefix, protocol)
# and its mode from shape_mode(shape) -- a mismatched wire/upstream pairing is
# structurally unrepresentable there, not just checked after the fact. What's left to
# verify here is coverage: that every declared ApiShape actually gets used somewhere.
def test_every_declared_shape_is_used() -> None:
    shapes_seen = {
        ApiShape(entry["model_name"].split("/")[1])
        for entry in main_proxy_config()["model_list"]
        if entry["model_name"].count("/") == 2
    }
    assert shapes_seen == set(ApiShape)


def test_tana_routes_register_the_in_process_provider() -> None:
    config = main_proxy_config()
    tana_entries = [entry for entry in config["model_list"] if entry["model_name"].startswith("tana/")]

    assert tana_entries
    assert all(entry["litellm_params"]["custom_llm_provider"] == "tana" for entry in tana_entries)
    assert any(item["provider"] == "tana" for item in config["litellm_settings"]["custom_provider_map"])


if __name__ == "__main__":
    pytest_bazel.main()
