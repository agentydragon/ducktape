"""Invariants over the generated main-proxy config."""

import pytest_bazel

from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.model_rosters import (
    LLAMA_CPP_MODELS,
    OLLAMA_CHAT_MODELS,
    ApiShape,
    Provider,
    exposed_name,
    ollama_chat_variant,
)


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


def test_ollama_native_chat_routes_keep_names_and_use_chat_adapter() -> None:
    """The public `olm-chat` contract stays stable while LiteLLM dispatches to `/api/chat`."""
    expected = {
        exposed_name(
            Provider.OLLAMA, ApiShape.OLM_CHAT, ollama_chat_variant(model, context)
        ): f"ollama_chat/{ollama_model}"
        for model, ollama_model, contexts in OLLAMA_CHAT_MODELS
        for context in contexts
    }
    native_entries = [
        entry for entry in main_proxy_config()["model_list"] if entry["model_name"].startswith("ollama/olm-chat/")
    ]

    assert len(native_entries) == len(expected)
    assert {entry["model_name"]: entry["litellm_params"]["model"] for entry in native_entries} == expected


def test_llama_cpp_routes_use_authenticated_openai_chat_backend() -> None:
    config = main_proxy_config()
    for model in LLAMA_CPP_MODELS:
        route = exposed_name(Provider.LLAMA_CPP, ApiShape.OAI_CHAT, model.id)
        [entry] = [item for item in config["model_list"] if item["model_name"] == route]

        assert entry["litellm_params"] == {
            "model": f"openai/{model.id}",
            "api_base": model.api_base,
            "api_key": "os.environ/LLAMA_CPP_API_KEY",
            "timeout": 600,
        }
        assert entry["model_info"] == {
            "mode": "chat",
            "supports_function_calling": True,
            "max_input_tokens": model.total_context_tokens - model.max_output_tokens,
            "max_output_tokens": model.max_output_tokens,
            "max_tokens": model.max_output_tokens,
        }


if __name__ == "__main__":
    pytest_bazel.main()
