"""Verify cross-file LiteLLM configuration wiring."""

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.litellm_config import main_proxy_config
from cluster.cdk8s.model_rosters import ApiShape
from cluster.validation.terraform_hcl import locals_blocks
from util.bazel.runfiles import get_required_path


# Terraform's model-key locals are also used below to verify that every
# allowlisted model is served by the LiteLLM proxy.
def _litellm_keys_locals() -> dict:
    blocks = locals_blocks(get_required_path("ducktape/tf/gitops/litellm-keys/main.tf"))
    return one(blocks)


# main.tf's own comment: "Model names must match generated model_name entries in
# cluster/k8s/litellm/app/litellm.k8s.yaml's embedded LiteLLM config". These are the
# remaining live-key locals that spell names out literally, so every element must
# resolve against the committed config rather than a second hand-maintained model
# reconstruction.
_TF_LITERAL_MODEL_LOCALS = [
    "oai_lane_models",
    "tana_client_models",
    "codex_client_models",
    "claude_client_models",
    "embedding_client_models",
    "gemini_client_models",
    "cheap_experiments_models",
]


def test_terraform_key_allowlists_only_name_models_the_proxy_serves() -> None:
    served = {entry["model_name"] for entry in main_proxy_config()["model_list"]}
    tf_locals = _litellm_keys_locals()
    for local in _TF_LITERAL_MODEL_LOCALS:
        missing = [model for model in tf_locals[local] if model not in served]
        assert not missing, f"{local} allows models the proxy does not serve: {missing}"


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
