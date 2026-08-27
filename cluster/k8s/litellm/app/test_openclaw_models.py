import json5
import pytest_bazel
import yaml

from cluster.k8s.litellm.app.model_rosters import (
    ANTHROPIC_MODELS,
    CLIPROXY_MODELS,
    GEMINI_CONTEXT_WINDOW,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MODELS,
    GEMINI_NON_REASONING_MODELS,
    OPENCLAW_CLIPROXY_MODELS,
    OPENCLAW_CODEX_MODELS,
    legacy_messages_name,
)
from util.bazel.runfiles import get_required_path

# Measured, not published, and the published figures are wrong in both
# directions: the raw models are ~1.05M and Codex product documentation says
# 272K, while this serving path (OpenClaw -> LiteLLM -> CLIProxyAPI -> upstream)
# accepts neither. LiteLLM carries no `max_input_tokens` for the codex-* routes,
# so there is nothing upstream of this file to consult instead.
#
# openai_utils/probe_context_window.py binary-searches the live path. On
# 2026-07-29 all three 5.6 models behaved identically: 370,629 counted tokens
# accepted, 372,194 rejected. Re-derive with:
#
#     kubectl exec -i -n <ns> <pod> -- python3 - --low 350000 --high 400000 \
#         codex-gpt-5.6-{luna,sol,terra} < openai_utils/probe_context_window.py
CODEX_CONTEXT_WINDOW = 372_000
CODEX_MAX_TOKENS = 128_000

_PUBLIC_CODER_AGENT_CONFIG = "ducktape/cluster/k8s/agents/public-coder-agent/app/openclaw.json"
_HAKU_OPENCLAW_CONFIG = "ducktape/cluster/k8s/agents/haku-openclaw-spike/app/openclaw.json"
_HAKU_OPENCLAW_DEPLOYMENT = "ducktape/cluster/k8s/agents/haku-openclaw-spike/app/deployment.yaml"
_LITELLM_CONFIG = "ducktape/cluster/k8s/litellm/app/proxy-config.yaml"
_LITELLM_KEYS_TF = "ducktape/tf/gitops/litellm-keys/main.tf"
_TANA_MODELS = {
    "tana-claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "tana-claude-opus-4-6": "claude-opus-4-6/high",
    "tana-claude-sonnet-4-6": "claude-sonnet-4-6/medium",
}


def _public_coder_agent_models() -> list[dict]:
    config = json5.loads(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    models: list[dict] = config["models"]["providers"]["litellm-subscription"]["models"]
    return models


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


def test_litellm_config_has_a_route_per_declared_codex_model() -> None:
    """Every model the agents may name must exist in the committed LiteLLM config."""
    assert set(OPENCLAW_CLIPROXY_MODELS) <= set(CLIPROXY_MODELS)
    litellm_models = _litellm_models()
    for model in OPENCLAW_CLIPROXY_MODELS:
        assert litellm_models[legacy_messages_name(model)] == {
            "model_name": legacy_messages_name(model),
            "litellm_params": {
                "model": f"anthropic/{model}",
                "api_base": "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317",
                "api_key": "os.environ/CLIPROXY_CLIENT_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def test_current_anthropic_roster_matches_haku_openclaw() -> None:
    assert ANTHROPIC_MODELS == ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"]

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
        assert litellm_models[model_id] == {
            "model_name": model_id,
            "litellm_params": {"model": f"anthropic/{model_id}", "api_key": "os.environ/ANTHROPIC_API_KEY"},
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def test_tana_compatibility_roster_remains_explicitly_pinned() -> None:
    litellm_models = _litellm_models()

    assert {name for name in litellm_models if name.startswith("tana-claude-")} == _TANA_MODELS.keys()
    for exposed, downstream in _TANA_MODELS.items():
        assert litellm_models[exposed] == {
            "model_name": exposed,
            "litellm_params": {
                "model": f"anthropic/{downstream}",
                "api_base": "http://tana-litellm.litellm.svc.cluster.local:4000",
                "api_key": "os.environ/LITELLM_MASTER_KEY",
            },
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def test_public_coder_agent_models_match_litellm_codex_routes() -> None:
    """The agent's catalog is pinned to exactly the Codex and Gemini routes it should offer."""
    assert [model["id"] for model in _public_coder_agent_models()] == [*OPENCLAW_CODEX_MODELS, *GEMINI_MODELS]

    config = json5.loads(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    provider = config["models"]["providers"]["litellm-subscription"]
    assert provider["api"] == "anthropic-messages"
    assert config["agents"]["defaults"]["model"]["primary"] in {
        f"litellm-subscription/{model_id}" for model_id in OPENCLAW_CODEX_MODELS
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
    """Gemini catalog entries route to a committed LiteLLM model and carry Google's published limits."""
    litellm_models = _litellm_models()
    models = {model["id"]: model for model in _public_coder_agent_models()}

    for model_id in GEMINI_MODELS:
        assert model_id in litellm_models, f"{model_id} has no committed LiteLLM route"
        entry = models[model_id]
        assert entry["contextWindow"] == GEMINI_CONTEXT_WINDOW
        assert entry["maxTokens"] == GEMINI_MAX_OUTPUT_TOKENS
        assert entry["input"] == ["text", "image"]
        assert entry["reasoning"] == (model_id not in GEMINI_NON_REASONING_MODELS)
    # maxTokens is reserved out of the window, so it has to leave room for input.
    assert GEMINI_MAX_OUTPUT_TOKENS < GEMINI_CONTEXT_WINDOW


if __name__ == "__main__":
    pytest_bazel.main()
