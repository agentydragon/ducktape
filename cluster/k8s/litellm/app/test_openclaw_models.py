import json5
import pytest_bazel
import yaml

from cluster.k8s.litellm.app.model_rosters import (
    ANTHROPIC_MODELS,
    CLIPROXY_MODELS,
    CODEX_CONTEXT_WINDOW,
    CODEX_MAX_TOKENS,
    GEMINI_CONTEXT_WINDOW,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MODELS,
    GEMINI_NON_REASONING_MODELS,
    OPENCLAW_CLIPROXY_MODELS,
    OPENCLAW_CODEX_MODELS,
    ApiShape,
    Provider,
    exposed_name,
)
from util.bazel.runfiles import get_required_path

_PUBLIC_CODER_AGENT_CONFIG = "ducktape/cluster/k8s/agents/public-coder-agent/app/openclaw.json5"
_HAKU_OPENCLAW_CONFIG = "ducktape/cluster/k8s/agents/haku-openclaw-spike/app/openclaw.json"
_HAKU_OPENCLAW_DEPLOYMENT = "ducktape/cluster/k8s/agents/haku-openclaw-spike/app/deployment.yaml"
_LITELLM_CONFIG = "ducktape/cluster/k8s/litellm/app/proxy-config.yaml"


def _public_coder_agent_models() -> list[dict]:
    config = json5.loads(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    providers = config["models"]["providers"]
    return [model for provider in providers.values() for model in provider["models"]]


def _haku_claude_models() -> tuple[dict, dict]:
    config = json5.loads(get_required_path(_HAKU_OPENCLAW_CONFIG).read_text())
    return config, config["agents"]["defaults"]["models"]


def _haku_openclaw_env() -> dict[str, str]:
    deployment = yaml.safe_load(get_required_path(_HAKU_OPENCLAW_DEPLOYMENT).read_text())
    container = next(
        entry for entry in deployment["spec"]["template"]["spec"]["containers"] if entry["name"] == "openclaw"
    )
    return {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}


def _litellm_models() -> dict[str, dict]:
    config = yaml.safe_load(get_required_path(_LITELLM_CONFIG).read_text())
    return {entry["model_name"]: entry for entry in config["model_list"]}


_OPENCLAW_GEMINI_IDS = [exposed_name(Provider.GOOGLE, ApiShape.OAI_CHAT, model) for model in GEMINI_MODELS]


def test_litellm_config_has_a_route_per_declared_codex_model() -> None:
    """Every model the agents may name must exist in the committed LiteLLM config."""
    assert set(OPENCLAW_CLIPROXY_MODELS) <= set(CLIPROXY_MODELS)
    litellm_models = _litellm_models()
    for model, catalog_id in zip(OPENCLAW_CLIPROXY_MODELS, OPENCLAW_CODEX_MODELS, strict=True):
        assert litellm_models[catalog_id] == {
            "model_name": catalog_id,
            "litellm_params": {
                "model": f"openai/{model}",
                "api_base": "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317/v1",
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {
                "mode": "responses",
                "supports_function_calling": True,
                "max_input_tokens": CODEX_CONTEXT_WINDOW,
                "max_output_tokens": CODEX_MAX_TOKENS,
                "max_tokens": CODEX_MAX_TOKENS,
            },
        }


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


def test_public_coder_agent_models_match_litellm_codex_routes() -> None:
    """The agent's catalog is pinned to exactly the working routes it should offer."""
    assert [model["id"] for model in _public_coder_agent_models()] == [*OPENCLAW_CODEX_MODELS, *_OPENCLAW_GEMINI_IDS]

    config = json5.loads(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    providers = config["models"]["providers"]
    assert providers["litellm"]["api"] == "openai-responses"
    assert set(providers) == {"litellm"}
    assert [model["id"] for model in providers["litellm"]["models"]] == [*OPENCLAW_CODEX_MODELS, *_OPENCLAW_GEMINI_IDS]
    assert config["agents"]["defaults"]["model"]["primary"] in {
        f"litellm/{model_id}" for model_id in OPENCLAW_CODEX_MODELS
    }


def test_public_coder_memory_model_preserves_its_litellm_compatibility_alias() -> None:
    """Renaming the persisted model identity would force a full index rebuild."""
    config = json5.loads(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    model = config["memory"]["search"]["model"]

    assert model == "gemini-embedding-2"
    assert _litellm_models()[model] == {
        "model_name": model,
        "litellm_params": {"model": "gemini/gemini-embedding-2", "api_key": "os.environ/GEMINI_API_KEY"},
        "model_info": {"mode": "embedding"},
    }


def test_codex_context_window_is_the_measured_one() -> None:
    """The declared window must be the measured one, not a plausible-looking guess.

    Not a change-detector: the manifest had drifted to 200000/64000 -- a value
    that was both inconsistent and wrong -- before this check existed. This
    guarded two independently authored manifests against disagreeing until the
    OpenClaw gateway was deleted on 2026-07-31; `public-coder-agent` is the only
    declaring manifest now, so it guards against regression rather than drift.
    """
    models = {model["id"]: model for model in _public_coder_agent_models()}
    declared = [models[model_id] for model_id in OPENCLAW_CODEX_MODELS]

    assert [model["contextWindow"] for model in declared] == [CODEX_CONTEXT_WINDOW] * len(declared)
    assert [model["maxTokens"] for model in declared] == [CODEX_MAX_TOKENS] * len(declared)
    # maxTokens is reserved out of the window, so it has to leave room for input.
    assert CODEX_MAX_TOKENS < CODEX_CONTEXT_WINDOW


def test_gemini_models_match_the_published_spec() -> None:
    """Gemini catalog entries route to committed LiteLLM models and carry published limits."""
    litellm_models = _litellm_models()
    models = {model["id"]: model for model in _public_coder_agent_models()}

    for model_id, catalog_id in zip(GEMINI_MODELS, _OPENCLAW_GEMINI_IDS, strict=True):
        assert catalog_id in litellm_models, f"{catalog_id} has no committed LiteLLM route"
        entry = models[catalog_id]
        assert entry["contextWindow"] == GEMINI_CONTEXT_WINDOW
        assert entry["maxTokens"] == GEMINI_MAX_OUTPUT_TOKENS
        assert entry["input"] == ["text", "image"]
        assert entry["reasoning"] == (model_id not in GEMINI_NON_REASONING_MODELS)
    assert GEMINI_MAX_OUTPUT_TOKENS < GEMINI_CONTEXT_WINDOW


if __name__ == "__main__":
    pytest_bazel.main()
