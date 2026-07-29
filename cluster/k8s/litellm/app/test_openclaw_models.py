import json

import pytest_bazel
import yaml

from cluster.k8s.litellm.app.generate_litellm import OPENCLAW_CODEX_MODELS, generate
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

_PUBLIC_CODER_AGENT_CONFIG = "ducktape/cluster/k8s/agents/public-coder-agent/app/openclaw-config.yaml"


def _public_coder_agent_codex_models() -> list[dict]:
    doc = yaml.safe_load(get_required_path(_PUBLIC_CODER_AGENT_CONFIG).read_text())
    config = json.loads(doc["data"]["openclaw.json"])
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
            "model_info": {"mode": "chat", "supports_function_calling": True},
        }


def test_public_coder_agent_models_match_litellm_codex_routes() -> None:
    """The second agent's catalog is pinned to the same routes as the first."""
    assert [model["id"] for model in _public_coder_agent_codex_models()] == OPENCLAW_CODEX_MODELS


def test_codex_context_window_is_the_measured_one() -> None:
    """Both agents must declare the measured window, and must not drift apart.

    Not a change-detector: these are two independently authored manifests that
    have to agree. They had already drifted to 200000/64000 -- a value that was
    both inconsistent and wrong -- before this check existed.
    """
    openclaw = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/agents/openclaw/gateway/openclawinstance.yaml").read_text()
    )
    declared = openclaw["spec"]["config"]["raw"]["models"]["providers"]["litellm-subscription"]["models"]
    declared += _public_coder_agent_codex_models()

    assert [model["contextWindow"] for model in declared] == [CODEX_CONTEXT_WINDOW] * len(declared)
    assert [model["maxTokens"] for model in declared] == [CODEX_MAX_TOKENS] * len(declared)
    # maxTokens is reserved out of the window, so it has to leave room for input.
    assert CODEX_MAX_TOKENS < CODEX_CONTEXT_WINDOW


if __name__ == "__main__":
    pytest_bazel.main()
