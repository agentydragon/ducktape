"""Both thread presets receive the same public ducktape contribution procedure, and each sandbox
preset binds the Action policy sets its principal is meant to hold.

The preset is what a *launch* binds: a managed runner Pod runs as its own per-Sandbox
ServiceAccount, so a binding written for a static caller account never reaches it. A preset that
forgets a set the account binding names leaves those Actions approval-gated in exactly one of the
two places they are supposed to work, which reads as flaky policy rather than a missing grant.
"""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.actions_staging_policies import (
    DUCKTAPE_PR_FAILED_JOBS_SET,
    FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET,
    FINANCE_AGENT_GAFFER_PR_CREATION_SET,
)
from cluster.cdk8s.agentplane.app_settings import _PUBLIC_CODER_INSTRUCTIONS, DUCKTAPE_PR_INSTRUCTIONS
from cluster.cdk8s.agentplane.staging_config import _FINANCE_AGENT_INSTRUCTIONS, HAKU_ACTION_POLICY_SETS


def test_ducktape_pr_instructions_are_shared_once() -> None:
    for instructions in (_PUBLIC_CODER_INSTRUCTIONS, _FINANCE_AGENT_INSTRUCTIONS):
        assert instructions.count(DUCKTAPE_PR_INSTRUCTIONS) == 1
        assert "agentydragon-agent/ducktape" in instructions
        assert "agentydragon/ducktape" in instructions


def test_gaffer_write_policies_are_finance_agent_only() -> None:
    cfg = staging.ENV.app_config
    finance_policies = cfg.sandbox_presets["finance-agent"].action_policy_sets
    assert FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET in finance_policies
    assert FINANCE_AGENT_GAFFER_PR_CREATION_SET in finance_policies
    for name, preset in cfg.sandbox_presets.items():
        if name == "finance-agent":
            continue
        policies = preset.action_policy_sets
        assert FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET not in policies
        assert FINANCE_AGENT_GAFFER_PR_CREATION_SET not in policies


def test_haku_preset_binds_what_its_account_binding_names(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    """The Haku preset covers every set the `haku-agent-reads` binding describes, plus Haku's one
    write. The binding is the reviewed bundle; the preset is what a launch actually gets, because a
    managed runner Pod runs as its own per-Sandbox ServiceAccount and never sees that binding."""
    docs = agentplane_manifests[staging.ENV.namespace]
    binding = one(
        doc for doc in docs if doc["kind"] == "ActionPolicyBinding" and doc["metadata"]["name"] == "haku-agent-reads"
    )
    bound = set(binding["spec"]["policySets"])
    app_config = one(
        doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-app-config"
    )
    preset = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]["haku"]["action_policy_sets"]
    assert bound <= set(preset), f"preset is missing {sorted(bound - set(preset))}"
    assert set(preset) == set(HAKU_ACTION_POLICY_SETS), "preset and its named bundle drifted"
    assert set(preset) - bound == {DUCKTAPE_PR_FAILED_JOBS_SET}, "a second write joined the bundle"
    assert len(preset) == len(set(preset))

    # Every name the preset binds must resolve to a set this namespace actually creates, or the
    # Sandbox Service refuses the launch outright.
    created = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "ActionPolicySet"}
    assert set(preset) <= created, sorted(set(preset) - created)


if __name__ == "__main__":
    pytest_bazel.main()
