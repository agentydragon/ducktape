"""Sandbox presets bind the Action policy sets their principals are meant to hold."""

from typing import Any

import pytest
import pytest_bazel
import yaml

from cluster.cdk8s.agentplane import kubernetes_account_ref, staging
from cluster.cdk8s.agentplane.actions_staging_policies import (
    DUCKTAPE_PR_FAILED_JOBS_SET,
    GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET,
)
from cluster.cdk8s.agentplane.egress import BASIC_POLICY
from cluster.cdk8s.agentplane.staging_config import HAKU_ACTION_POLICY_SETS
from cluster.cdk8s.testing.manifests import find_resource


def test_gaffer_write_policies_are_finance_agent_only() -> None:
    assert staging.ENV.app is not None
    cfg = staging.ENV.app.config
    finance_policies = cfg.sandbox_presets["finance-agent"].policy_sets
    assert GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET in finance_policies
    for name, preset in cfg.sandbox_presets.items():
        if name == "finance-agent":
            continue
        policies = preset.policy_sets
        assert GAFFER_PRIVATE_BRANCH_AND_PR_CREATION_SET not in policies


def test_haku_preset_binds_what_its_account_binding_names(staging_manifests: list[dict[str, Any]]) -> None:
    """The Haku preset covers every Action set the `haku-agent` binding names, plus Haku's one
    write. The binding is the reviewed bundle; a launch's Identity has its own binding, so the
    preset spells out what that Sandbox receives."""
    binding = find_resource(staging_manifests, "PolicyBinding", "haku-agent")
    app_config = find_resource(staging_manifests, "ConfigMap", "app-config")
    preset = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]["haku"]["policy_sets"]
    assert len(preset) == len(set(preset))
    action_sets = {
        doc["metadata"]["name"]
        for doc in staging_manifests
        if doc["kind"] == "PolicySet" and any(rule["type"] == "action_auto_approve" for rule in doc["spec"]["rules"])
    }
    bound = set(binding["spec"]["policySets"]) & action_sets
    bound -= {BASIC_POLICY}
    assert bound <= set(preset), f"preset is missing {sorted(bound - set(preset))}"
    # `basic` owns the same-principal Session-delegation approval rule, while also serving direct
    # platform ServiceAccount callers.
    expected_action_sets = set(HAKU_ACTION_POLICY_SETS) | {BASIC_POLICY}
    assert set(preset) & action_sets == expected_action_sets, "preset and its named bundle drifted"
    assert set(HAKU_ACTION_POLICY_SETS) - bound == {DUCKTAPE_PR_FAILED_JOBS_SET}, "a second write joined the bundle"

    # Every name the preset binds must resolve to a PolicySet this namespace actually creates, or the
    # The app refuses the launch outright.
    created = {doc["metadata"]["name"] for doc in staging_manifests if doc["kind"] == "PolicySet"}
    assert set(preset) <= created, sorted(set(preset) - created)


@pytest.mark.parametrize("policy_set", ["quest-scheduler-read", "thrive-read"])
def test_read_policy_grants(staging_manifests: list[dict[str, Any]], policy_set: str) -> None:
    for name in ("haku-agent", "claude-ai"):
        binding = find_resource(staging_manifests, "PolicyBinding", name)
        assert policy_set in binding["spec"]["policySets"]
    app_config = find_resource(staging_manifests, "ConfigMap", "app-config")
    presets = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]
    assert policy_set in presets["haku"]["policy_sets"]
    assert all(policy_set not in preset["policy_sets"] for name, preset in presets.items() if name != "haku")


def test_haku_and_claude_account_policy_order(staging_manifests: list[dict[str, Any]]) -> None:
    bindings = {
        doc["metadata"]["name"]: doc["spec"]["policySets"]
        for doc in staging_manifests
        if doc["kind"] == "PolicyBinding" and doc["metadata"]["name"] in {"haku-agent", "claude-ai"}
    }
    claude_only = {
        "buildbuddy",
        "mimir-reads",
        "orchestration-presets",
        kubernetes_account_ref.account(staging.ENV.namespace, "claude-ai").credential_name,
    }
    haku, claude = bindings["haku-agent"], bindings["claude-ai"]
    haku_account_use = kubernetes_account_ref.account(staging.ENV.namespace, "haku-agent").credential_name
    claude_account_use = kubernetes_account_ref.account(staging.ENV.namespace, "claude-ai").credential_name
    assert len(haku) == len(set(haku))
    assert len(claude) == len(set(claude))
    assert set(claude) - set(haku) == claude_only
    assert set(haku) - set(claude) == {haku_account_use}
    assert haku[-1] == haku_account_use
    assert claude[-1] == claude_account_use
    assert [policy for policy in claude if policy not in claude_only] == [
        policy for policy in haku if policy != haku_account_use
    ]


if __name__ == "__main__":
    pytest_bazel.main()
