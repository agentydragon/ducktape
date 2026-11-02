"""haku-anki's egress identity: AnkiWeb and nothing else, per workload."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.action_service.policies.resources import CALLER_LABEL
from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.haku import anki_egress, rbac as haku_rbac


@pytest.fixture(scope="module")
def resources(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    app = scratch_app_factory()
    return [
        doc
        for chart in (staging.chart(app), haku_rbac.chart(app))
        for doc in cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    ]


def _named(resources: list[dict[str, Any]], kind: str, name: str, namespace: str) -> dict[str, Any]:
    return one(
        d
        for d in resources
        if d["kind"] == kind and d["metadata"]["name"] == name and d["metadata"].get("namespace") == namespace
    )


def test_service_account_is_not_an_action_caller_and_mounts_no_token(resources: list[dict[str, Any]]) -> None:
    account = _named(resources, "ServiceAccount", anki_egress.SERVICE_ACCOUNT, anki_egress.NAMESPACE)

    assert CALLER_LABEL not in account["metadata"].get("labels", {})
    assert account["automountServiceAccountToken"] is False


def test_policy_reaches_ankiweb_only_and_presents_no_credential(resources: list[dict[str, Any]]) -> None:
    policy = _named(resources, "PolicySet", anki_egress.POLICY, "agentplane-staging")
    (rule,) = policy["spec"]["rules"]

    assert rule["type"] == "egress"
    assert rule["hosts"] == ["*.ankiweb.net"]
    # The real AnkiWeb credentials stay in the Pod; nothing is substituted, narrowed or cluster-internal.
    for absent in ("credentialRef", "methods", "paths", "clusterInternal"):
        assert absent not in rule, absent


def test_binding_grants_that_one_policy_to_that_one_account(resources: list[dict[str, Any]]) -> None:
    binding = _named(resources, "PolicyBinding", anki_egress.SERVICE_ACCOUNT, "agentplane-staging")

    assert binding["spec"]["subject"] == {"namespace": anki_egress.NAMESPACE, "name": anki_egress.SERVICE_ACCOUNT}
    assert binding["spec"]["policySets"] == [anki_egress.POLICY]


def test_gateway_admits_only_that_account_from_haku_sandbox(resources: list[dict[str, Any]]) -> None:
    policy = _named(resources, "CiliumNetworkPolicy", anki_egress.POLICY, "agentplane-staging")
    (ingress,) = policy["spec"]["ingress"]
    (peer,) = ingress["fromEndpoints"]

    assert peer["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": anki_egress.NAMESPACE,
        "io.cilium.k8s.policy.serviceaccount": anki_egress.SERVICE_ACCOUNT,
    }
    assert [p["port"] for p in one(ingress["toPorts"])["ports"]] == ["8888"]


if __name__ == "__main__":
    pytest_bazel.main()
