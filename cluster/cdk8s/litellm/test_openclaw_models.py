from dataclasses import replace

import pytest
import pytest_bazel

from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.public_coder import config as public_coder_config
from model_catalog.catalog import GPT6_ASTRA_RESPONSES


def _public_coder_agent_models() -> list[dict]:
    providers = public_coder_config.config()["models"]["providers"]
    return [model for provider in providers.values() for model in provider["models"]]


def _litellm_models() -> dict[str, dict]:
    return {entry["model_name"]: entry for entry in main_proxy_config()["model_list"]}


def test_public_coder_agent_catalog_names_only_served_routes() -> None:
    """OpenClaw's bundled LiteLLM provider never queries the proxy's /v1/models, so every
    catalog id must be a route the proxy serves."""
    served = _litellm_models()
    for model in _public_coder_agent_models():
        assert model["id"] in served, f"{model['id']} has no LiteLLM route"
        assert model["maxTokens"] < model["contextWindow"]


def test_public_coder_rejects_unknown_reasoning_capability() -> None:
    route = GPT6_ASTRA_RESPONSES
    with pytest.raises(ValueError, match="missing OpenClaw metadata"):
        public_coder_config._model_entry(
            replace(route, model=replace(route.model, reasoning=None)), context_budget=128_000, output_budget=16_000
        )


def test_public_coder_memory_model_is_a_served_embedding_route() -> None:
    model = public_coder_config.config()["memory"]["search"]["model"]
    served = _litellm_models()

    assert model in served
    assert served[model]["model_info"]["mode"] == "embedding"


if __name__ == "__main__":
    pytest_bazel.main()
