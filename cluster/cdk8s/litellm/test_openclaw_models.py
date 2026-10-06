from dataclasses import replace

import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s import public_coder_agent_config
from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.parked import haku_openclaw_spike_config
from model_catalog.catalog import ANTHROPIC_SUBSCRIPTION_ROUTES, GPT6_ASTRA_RESPONSES


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
    catalog id must be a route the proxy serves."""
    served = _litellm_models()
    for model in _public_coder_agent_models():
        assert model["id"] in served, f"{model['id']} has no LiteLLM route"
        assert model["maxTokens"] < model["contextWindow"]


def test_public_coder_rejects_unknown_reasoning_capability() -> None:
    route = GPT6_ASTRA_RESPONSES
    with pytest.raises(ValueError, match="missing OpenClaw metadata"):
        public_coder_agent_config._model_entry(
            replace(route, model=replace(route.model, reasoning=None)), context_budget=128_000, output_budget=16_000
        )


def test_current_anthropic_roster_matches_haku_openclaw() -> None:
    config, models = _haku_claude_models()
    expected_refs = {f"anthropic/{route.model.id}" for route in ANTHROPIC_SUBSCRIPTION_ROUTES}
    defaults = config["agents"]["defaults"]

    # The shared roster, selectable policy, and configured catalog must describe
    # the same models; the first roster entry is the intentional default.
    assert set(models) == expected_refs
    assert set(defaults["modelPolicy"]["allow"]) == expected_refs
    assert defaults["model"]["primary"] == f"anthropic/{ANTHROPIC_SUBSCRIPTION_ROUTES[0].model.id}"

    # These Anthropic refs are subscription-backed Claude Code invocations, not
    # direct Anthropic API calls. Keep runtime, plugin ownership, and auth aligned.
    assert {entry["agentRuntime"]["id"] for entry in models.values()} == {"claude-cli"}
    assert config["plugins"]["entries"]["anthropic"]["enabled"] is True
    assert "auth" not in config
    haku_env = _haku_openclaw_env()
    assert haku_env["OPENCLAW_LIVE_CLI_BACKEND_PRESERVE_ENV"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert haku_env["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-")
    assert haku_env["GH_PAT"] == "proxy-github-placeholder"


def test_public_coder_memory_model_is_a_served_embedding_route() -> None:
    model = public_coder_agent_config.config()["memory"]["search"]["model"]
    served = _litellm_models()

    assert model in served
    assert served[model]["model_info"]["mode"] == "embedding"


if __name__ == "__main__":
    pytest_bazel.main()
