import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s import haku_openclaw_spike_config, public_coder_agent_config
from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.model_rosters import (
    ANTHROPIC_MODELS,
    GEMINI_CONTEXT_WINDOW,
    GEMINI_MAX_OUTPUT_TOKENS,
    OPENCLAW_CODEX_MODELS,
    ApiShape,
    Provider,
    exposed_name,
)


def _public_coder_agent_models() -> list[dict]:
    providers = public_coder_agent_config.config()["models"]["providers"]
    return [model for provider in providers.values() for model in provider["models"]]


def _haku_claude_models() -> tuple[dict, dict]:
    config = haku_openclaw_spike_config.config()
    return config, config["agents"]["defaults"]["models"]


def _haku_openclaw_env() -> dict[str, str]:
    manifests = Cdk8sTesting.synth(haku_openclaw_spike_config.app_chart(Cdk8sTesting.app()))
    deployment = next(manifest for manifest in manifests if manifest["kind"] == "Deployment")
    container = next(
        entry for entry in deployment["spec"]["template"]["spec"]["containers"] if entry["name"] == "openclaw"
    )
    return {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}


def _litellm_models() -> dict[str, dict]:
    return {entry["model_name"]: entry for entry in main_proxy_config()["model_list"]}


def test_public_coder_agent_catalog_names_only_served_routes() -> None:
    """OpenClaw's bundled LiteLLM provider never queries the proxy's /v1/models, so every
    catalog id must be a route the proxy serves. Not by construction: the OpenClaw Codex
    subset and CLIPROXY_MODELS are two rosters, and the catalog derives its names with
    codex_responses_name() while the proxy derives them through shape_for()."""
    served = _litellm_models()
    for model in _public_coder_agent_models():
        assert model["id"] in served, f"{model['id']} has no LiteLLM route"


def test_catalog_limits_leave_room_for_input() -> None:
    # maxTokens is reserved out of the (measured or published) context window.
    for model in OPENCLAW_CODEX_MODELS:
        assert model.max_tokens < model.context_window, model.id
    assert GEMINI_MAX_OUTPUT_TOKENS < GEMINI_CONTEXT_WINDOW


def test_current_anthropic_roster_matches_haku_openclaw() -> None:
    config, models = _haku_claude_models()
    expected_refs = {f"anthropic/{model_id}" for model_id in ANTHROPIC_MODELS}
    defaults = config["agents"]["defaults"]

    # The shared roster, selectable policy, and configured catalog must describe
    # the same models; the first roster entry is the intentional default.
    assert set(models) == expected_refs
    assert set(defaults["modelPolicy"]["allow"]) == expected_refs
    assert defaults["model"]["primary"] == f"anthropic/{ANTHROPIC_MODELS[0]}"

    # These Anthropic refs are subscription-backed Claude Code invocations, not
    # direct Anthropic API calls. Keep runtime, plugin ownership, and auth aligned.
    assert {entry["agentRuntime"]["id"] for entry in models.values()} == {"claude-cli"}
    assert config["plugins"]["entries"]["anthropic"]["enabled"] is True
    assert "auth" not in config
    haku_env = _haku_openclaw_env()
    assert haku_env["OPENCLAW_LIVE_CLI_BACKEND_PRESERVE_ENV"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert haku_env["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-")
    assert haku_env["GH_PAT"] == "proxy-github-placeholder"

    litellm_models = _litellm_models()
    for model_id in ANTHROPIC_MODELS:
        model_name = exposed_name(Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, model_id)
        assert litellm_models[model_name] == {
            "model_name": model_name,
            "litellm_params": {"model": f"anthropic/{model_id}", "api_key": "os.environ/ANTHROPIC_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def test_public_coder_memory_model_uses_ollama_embedding_route() -> None:
    """The OpenClaw model identity must match the Ollama embedding route."""
    config = public_coder_agent_config.config()
    model = config["memory"]["search"]["model"]

    assert model == "ollama/olm-embed/qwen3-embedding-4b"
    assert _litellm_models()[model] == {
        "model_name": model,
        "litellm_params": {
            "model": "ollama/qwen3-embedding:4b",
            "api_base": "http://ollama.ollama.svc.cluster.local:11434",
        },
        "model_info": {"mode": "embedding"},
    }


if __name__ == "__main__":
    pytest_bazel.main()
