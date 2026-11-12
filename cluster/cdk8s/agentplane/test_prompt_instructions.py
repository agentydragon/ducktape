"""Both thread presets receive the same public ducktape contribution procedure, and each sandbox
preset binds the Action policy sets its principal is meant to hold.

The preset is what a *launch* binds: a managed runner Pod runs as its own per-Sandbox
ServiceAccount, so a binding written for a static caller account never reaches it. A preset that
forgets a set the account binding names leaves those Actions approval-gated in exactly one of the
two places they are supposed to work, which reads as flaky policy rather than a missing grant.
"""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.actions_staging_policies import (
    DUCKTAPE_PR_FAILED_JOBS_SET,
    GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET,
)
from cluster.cdk8s.agentplane.app_settings import _PUBLIC_CODER_INSTRUCTIONS, DUCKTAPE_PR_INSTRUCTIONS
from cluster.cdk8s.agentplane.egress import BASIC_IDENTITY_POLICY, BASIC_POLICY
from cluster.cdk8s.agentplane.staging_config import _FINANCE_AGENT_INSTRUCTIONS, HAKU_ACTION_POLICY_SETS


def test_ducktape_pr_instructions_are_shared_once() -> None:
    for instructions in (_PUBLIC_CODER_INSTRUCTIONS, _FINANCE_AGENT_INSTRUCTIONS):
        assert instructions.count(DUCKTAPE_PR_INSTRUCTIONS) == 1


def test_ducktape_preset_installs_hooks_without_direnv() -> None:
    cfg = staging.ENV.app_config
    script = cfg.thread_presets["public-coder-ducktape-codex"].setup_script
    assert "pre-commit install" in script
    assert "direnv allow" not in script
    # The generic public coder preset has no repo-specific checkout or hooks.
    assert cfg.thread_presets["public-coder-codex"].setup_script == ""


def test_gaffer_write_policies_are_finance_agent_only() -> None:
    cfg = staging.ENV.app_config
    finance_policies = cfg.sandbox_presets["finance-agent"].policy_sets
    assert GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET in finance_policies
    for name, preset in cfg.sandbox_presets.items():
        if name == "finance-agent":
            continue
        policies = preset.policy_sets
        assert GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET not in policies


def test_haku_preset_binds_what_its_account_binding_names(staging_manifests: list[dict[str, Any]]) -> None:
    """The Haku preset covers every Action set the `haku-agent` binding names, plus Haku's one
    write. The binding is the reviewed bundle; the preset is what a launch actually gets, because a
    managed runner Pod runs as its own per-Sandbox ServiceAccount and never sees that binding."""
    binding = one(
        doc for doc in staging_manifests if doc["kind"] == "PolicyBinding" and doc["metadata"]["name"] == "haku-agent"
    )
    app_config = one(
        doc for doc in staging_manifests if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "app-config"
    )
    preset = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]["haku"]["policy_sets"]
    assert len(preset) == len(set(preset))
    action_sets = {
        doc["metadata"]["name"]
        for doc in staging_manifests
        if doc["kind"] == "PolicySet" and any(rule["type"] == "action_auto_approve" for rule in doc["spec"]["rules"])
    }
    bound = set(binding["spec"]["policySets"]) & action_sets
    # Static ServiceAccount bindings keep `basic`; launch presets use its Identity-safe sibling.
    bound -= {BASIC_POLICY, BASIC_IDENTITY_POLICY}
    assert bound <= set(preset), f"preset is missing {sorted(bound - set(preset))}"
    # The Identity-safe basic sibling owns the same-principal Session-delegation approval rule,
    # so include it in the rendered action-set view without duplicating it in HAKU_ACTION_POLICY_SETS.
    expected_action_sets = set(HAKU_ACTION_POLICY_SETS) | {BASIC_IDENTITY_POLICY}
    assert set(preset) & action_sets == expected_action_sets, "preset and its named bundle drifted"
    assert set(HAKU_ACTION_POLICY_SETS) - bound == {DUCKTAPE_PR_FAILED_JOBS_SET}, "a second write joined the bundle"

    # Every name the preset binds must resolve to a PolicySet this namespace actually creates, or the
    # The app refuses the launch outright.
    created = {doc["metadata"]["name"] for doc in staging_manifests if doc["kind"] == "PolicySet"}
    assert set(preset) <= created, sorted(set(preset) - created)


@pytest.mark.parametrize("policy_set", ["quest-read", "thrive-read"])
def test_read_policy_grants(staging_manifests: list[dict[str, Any]], policy_set: str) -> None:
    for name in ("haku-agent", "claude-ai"):
        binding = one(
            doc for doc in staging_manifests if doc["kind"] == "PolicyBinding" and doc["metadata"]["name"] == name
        )
        assert policy_set in binding["spec"]["policySets"]
    app_config = one(
        doc for doc in staging_manifests if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "app-config"
    )
    presets = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]
    assert policy_set in presets["haku"]["policy_sets"]
    assert all(policy_set not in preset["policy_sets"] for name, preset in presets.items() if name != "haku")


def test_haku_and_claude_account_policy_order(staging_manifests: list[dict[str, Any]]) -> None:
    bindings = {
        doc["metadata"]["name"]: doc["spec"]["policySets"]
        for doc in staging_manifests
        if doc["kind"] == "PolicyBinding" and doc["metadata"]["name"] in {"haku-agent", "claude-ai"}
    }
    claude_only = {"buildbuddy", "mimir-reads", "orchestration-presets"}
    haku, claude = bindings["haku-agent"], bindings["claude-ai"]
    assert len(haku) == len(set(haku))
    assert len(claude) == len(set(claude))
    assert set(claude) - set(haku) == claude_only
    assert [policy for policy in claude if policy not in claude_only] == haku


if __name__ == "__main__":
    pytest_bazel.main()
