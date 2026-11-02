"""haku-ui's agentplane principal: what it is granted, and that it stays within claude-ai's."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.action_service.policies.resources import CALLER_LABEL
from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.actions_staging_policies import ANKIWEB_SYNC_POLICY
from cluster.cdk8s.haku import rbac as haku_rbac, ui_egress


@pytest.fixture(scope="module")
def resources(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    app = scratch_app_factory()
    return [
        doc
        for chart in (staging.chart(app), haku_rbac.chart(app))
        for doc in cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    ]


def _binding(resources: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return one(d for d in resources if d["kind"] == "PolicyBinding" and d["metadata"]["name"] == name)


def test_haku_ui_is_a_labelled_caller_with_no_token_automount(resources: list[dict[str, Any]]) -> None:
    account = one(
        d
        for d in resources
        if d["kind"] == "ServiceAccount"
        and d["metadata"]["name"] == ui_egress.SERVICE_ACCOUNT
        and d["metadata"]["namespace"] == ui_egress.NAMESPACE
    )

    assert account["metadata"]["labels"][CALLER_LABEL] == "true"
    assert account["automountServiceAccountToken"] is False


def test_haku_ui_grants_are_a_subset_of_claude_ais(resources: list[dict[str, Any]]) -> None:
    """`haku-sandbox-admin` lets Haku run Pods as haku-ui, so its grants must add nothing to claude-ai's."""
    haku_ui = _binding(resources, ui_egress.SERVICE_ACCOUNT)
    claude_ai = _binding(resources, "claude-ai")

    assert haku_ui["spec"]["subject"] == {"namespace": ui_egress.NAMESPACE, "name": ui_egress.SERVICE_ACCOUNT}
    # haku-ui's own egress policy is new; AnkiWeb is the one deliberate grant claude-ai lacks (haku-anki runs as
    # this account, and Haku already reaches *.ankiweb.net through haku-egress-proxy; see actions_staging_policies.py).
    own = set(haku_ui["spec"]["policySets"]) - {ui_egress.POLICY, ANKIWEB_SYNC_POLICY}
    assert own <= set(claude_ai["spec"]["policySets"])


def test_haku_ui_policy_reaches_only_the_action_request_routes(resources: list[dict[str, Any]]) -> None:
    policy = one(d for d in resources if d["kind"] == "PolicySet" and d["metadata"]["name"] == ui_egress.POLICY)
    (rule,) = policy["spec"]["rules"]

    assert rule["methods"] == ["GET", "POST"]
    assert rule["paths"] == ["/v1/action-requests", "/v1/action-requests/**"]
    assert rule["credentialRef"] == {"name": "agentplane-workload"}


def _settings(resources: list[dict[str, Any]], config_map: str) -> dict[str, Any]:
    data = one(d for d in resources if d["kind"] == "ConfigMap" and d["metadata"]["name"] == config_map)["data"]
    return cast(dict[str, Any], yaml.safe_load(one(data.values())))


def test_haku_sandbox_is_an_egress_and_action_caller_namespace(resources: list[dict[str, Any]]) -> None:
    egress = _settings(resources, "egress-go-settings")
    actions = _settings(resources, "actions-settings")

    assert ui_egress.NAMESPACE in egress["allowed_service_account_namespaces"]
    assert ui_egress.NAMESPACE in actions["caller_service_account_namespaces"]
    # Callers are watched in every allowed namespace, but policies and bindings only in the staging one.
    assert actions["policy_namespace"] == "agentplane-staging"


if __name__ == "__main__":
    pytest_bazel.main()
