import json

import pytest_bazel
import yaml

from cluster.k8s.litellm.app.generate_litellm import (
    CODEX_CONTEXT_WINDOW,
    CODEX_MAX_OUTPUT_TOKENS,
    OPENCLAW_CODEX_MODELS,
    generate,
)
from util.bazel.runfiles import get_required_path

# LiteLLM is the single source of truth for these numbers (declared in
# generate_litellm.py, served at /v1/model/info). This test pins every consumer
# that does NOT read that endpoint -- OpenClaw's bundled provider does not query
# it, so both OpenClaw configs hardcode the values and can silently drift. They
# already had: both were at 200000/64000, which was inconsistent with each other
# and wrong against the measured window.

_PUBLIC_CODER_AGENT_CONFIG = "ducktape/cluster/k8s/agents/public-coder-agent/app/openclaw.json"


def _public_coder_agent_codex_models() -> list[dict]:
    config = json.loads(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    models: list[dict] = config["models"]["providers"]["litellm-subscription"]["models"]
    return models


def test_openclaw_models_match_litellm_codex_routes() -> None:
    """Keep OpenClaw's static catalog aligned with the generated LiteLLM routes."""
    openclaw = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/agents/openclaw/gateway/openclawinstance.yaml").read_text()
    )
    provider = openclaw["spec"]["config"]["raw"]["models"]["providers"]["litellm-subscription"]
    configured_ids = [model["id"] for model in provider["models"]]

    assert configured_ids == OPENCLAW_CODEX_MODELS
    assert provider["api"] == "anthropic-messages"
    assert openclaw["spec"]["config"]["raw"]["agents"]["defaults"]["model"]["primary"] in {
        f"litellm-subscription/{model_id}" for model_id in OPENCLAW_CODEX_MODELS
    }

    litellm_models = {entry["model_name"]: entry for entry in yaml.safe_load(generate())["model_list"]}
    for model_id in OPENCLAW_CODEX_MODELS:
        assert litellm_models[model_id] == {
            "model_name": model_id,
            "litellm_params": {
                "model": f"anthropic/{model_id.removeprefix('codex-')}",
                "api_base": "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317",
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {
                "mode": "chat",
                "supports_function_calling": True,
                "max_input_tokens": CODEX_CONTEXT_WINDOW,
                "max_output_tokens": CODEX_MAX_OUTPUT_TOKENS,
            },
        }


def test_public_coder_agent_models_match_litellm_codex_routes() -> None:
    """The second agent's catalog is pinned to the same routes as the first."""
    assert [model["id"] for model in _public_coder_agent_codex_models()] == OPENCLAW_CODEX_MODELS


def test_litellm_declares_the_codex_context_window() -> None:
    """LiteLLM must actually serve the numbers, or consumers cannot discover them."""
    litellm_models = {entry["model_name"]: entry for entry in yaml.safe_load(generate())["model_list"]}
    for model_id in OPENCLAW_CODEX_MODELS:
        info = litellm_models[model_id]["model_info"]
        assert info["max_input_tokens"] == CODEX_CONTEXT_WINDOW
        assert info["max_output_tokens"] == CODEX_MAX_OUTPUT_TOKENS


def test_hardcoding_consumers_match_litellm() -> None:
    """Pin every consumer that does not read LiteLLM's model-listing API.

    OpenClaw's bundled provider never queries /v1/model/info, so both configs
    restate the numbers and can drift from the proxy and from each other. They
    already had -- both sat at 200000/64000, inconsistent and wrong.
    """
    openclaw = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/agents/openclaw/gateway/openclawinstance.yaml").read_text()
    )
    declared = openclaw["spec"]["config"]["raw"]["models"]["providers"]["litellm-subscription"]["models"]
    declared += _public_coder_agent_codex_models()

    assert [model["contextWindow"] for model in declared] == [CODEX_CONTEXT_WINDOW] * len(declared)
    assert [model["maxTokens"] for model in declared] == [CODEX_MAX_OUTPUT_TOKENS] * len(declared)
    # maxTokens is reserved out of the window, so it has to leave room for input.
    assert CODEX_MAX_OUTPUT_TOKENS < CODEX_CONTEXT_WINDOW


if __name__ == "__main__":
    pytest_bazel.main()
