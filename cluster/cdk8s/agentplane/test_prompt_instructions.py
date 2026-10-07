"""Both thread presets receive the same public ducktape contribution procedure."""

import pytest_bazel

from cluster.cdk8s.agentplane.actions_staging_policies import (
    FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET,
    FINANCE_AGENT_GAFFER_PR_CREATION_SET,
)
from cluster.cdk8s.agentplane.app_settings import _PUBLIC_CODER_INSTRUCTIONS, DUCKTAPE_PR_INSTRUCTIONS
from cluster.cdk8s.agentplane.staging_config import _FINANCE_AGENT_INSTRUCTIONS, config


def test_ducktape_pr_instructions_are_shared_once() -> None:
    for instructions in (_PUBLIC_CODER_INSTRUCTIONS, _FINANCE_AGENT_INSTRUCTIONS):
        assert instructions.count(DUCKTAPE_PR_INSTRUCTIONS) == 1
        assert "agentydragon-agent/ducktape" in instructions
        assert "agentydragon/ducktape" in instructions


def test_gaffer_write_policies_are_finance_agent_only() -> None:
    cfg = config()
    finance_policies = cfg.sandbox_presets["finance-agent"].action_policy_sets
    assert FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET in finance_policies
    assert FINANCE_AGENT_GAFFER_PR_CREATION_SET in finance_policies
    for name, preset in cfg.sandbox_presets.items():
        if name == "finance-agent":
            continue
        policies = preset.action_policy_sets
        assert FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET not in policies
        assert FINANCE_AGENT_GAFFER_PR_CREATION_SET not in policies


if __name__ == "__main__":
    pytest_bazel.main()
