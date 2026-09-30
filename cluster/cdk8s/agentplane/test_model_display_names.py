"""Tests for model_display_names.py's agentplane-scoped display-name lookup."""

import pytest
import pytest_bazel

from cluster.cdk8s.agentplane.app_settings import reasoning_efforts
from cluster.cdk8s.agentplane.model_display_names import display_name
from cluster.cdk8s.litellm.keys import (
    ANTIGRAVITY_CLIENT_MODELS,
    CHEAP_EXPERIMENTS_CLAUDE_MODEL,
    CHEAP_EXPERIMENTS_CODEX_MODEL,
    CLAUDE_CLIENT_MODELS,
    GPT6_OAI_LANE_MODELS,
    OLLAMA_CHAT_CLIENT_MODELS,
)


# One case per distinct code path, not per model: a second hand-written dict entry or a
# second reuse of the same merged roster would just restate a literal already implied by
# test_every_route_agentplane_can_offer_resolves below.
@pytest.mark.parametrize(
    ("exposed_name", "expected"),
    [
        ("anthropic-max20/ant-messages/claude-sonnet-5", "Sonnet 5"),  # hand-written dict + prefix strip
        ("chatgpt/oai-responses/gpt-6-luna", "GPT-6 Luna"),  # reused from OPENCLAW_CODEX_MODELS
        ("ollama/oai-chat/gpt-oss-20b-512k", "GPT-OSS 20B (512K)"),  # computed suffix, "NK" branch
        ("ollama/olm-chat/gpt-oss-20b-1m", "GPT-OSS 20B (1M)"),  # computed suffix, "1M" branch
    ],
)
def test_resolves_a_known_route_to_its_marketing_name(exposed_name: str, expected: str) -> None:
    assert display_name(exposed_name) == expected


def test_raises_for_a_model_outside_the_curated_set() -> None:
    with pytest.raises(KeyError):
        display_name("anthropic-api/ant-messages/claude-3-opus-not-a-real-model")


@pytest.mark.parametrize(
    "offered",
    [
        CLAUDE_CLIENT_MODELS,
        ANTIGRAVITY_CLIENT_MODELS,
        GPT6_OAI_LANE_MODELS,
        OLLAMA_CHAT_CLIENT_MODELS,
        [CHEAP_EXPERIMENTS_CLAUDE_MODEL],
        [CHEAP_EXPERIMENTS_CODEX_MODEL],
    ],
)
def test_every_route_agentplane_can_offer_resolves(offered: list[str]) -> None:
    """Every lane app_settings.py actually feeds into a deployed catalog must resolve, or
    synth would raise deep inside settings(); catch it here with a clear message instead."""
    for exposed_name in offered:
        display_name(exposed_name)


if __name__ == "__main__":
    pytest_bazel.main()


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic-max20/ant-messages/claude-sonnet-5", ["low", "medium", "high", "max"]),
        ("antigravity/ant-messages/claude-opus-4-6-thinking", ["low", "medium", "high", "max"]),
        ("chatgpt/oai-responses/gpt-6-luna", ["minimal", "low", "medium", "high", "xhigh"]),
        ("ollama/oai-chat/gpt-oss-20b-128k", []),
    ],
)
def test_reasoning_efforts_match_the_model_route(model: str, expected: list[str]) -> None:
    assert reasoning_efforts(model) == expected
