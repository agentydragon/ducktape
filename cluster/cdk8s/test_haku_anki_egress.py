"""haku-anki's AnkiWeb egress: one PolicySet, carried by the shared haku-ui account."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.actions_staging_policies import ANKIWEB_SYNC_POLICY
from cluster.cdk8s.haku import ui_egress


@pytest.fixture(scope="module")
def resources(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(staging.chart(scratch_app_factory())))


def _named(resources: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(d for d in resources if d["kind"] == kind and d["metadata"]["name"] == name)


def test_policy_reaches_ankiweb_only_and_presents_no_credential(resources: list[dict[str, Any]]) -> None:
    policy = _named(resources, "PolicySet", ANKIWEB_SYNC_POLICY)
    (rule,) = policy["spec"]["rules"]

    assert rule["type"] == "egress"
    assert rule["hosts"] == ["*.ankiweb.net"]
    # The real AnkiWeb credentials stay in the Pod; nothing is substituted, narrowed or cluster-internal.
    for absent in ("credentialRef", "methods", "paths", "clusterInternal"):
        assert absent not in rule, absent


def test_the_shared_haku_ui_account_carries_it(resources: list[dict[str, Any]]) -> None:
    binding = _named(resources, "PolicyBinding", ui_egress.SERVICE_ACCOUNT)

    assert ANKIWEB_SYNC_POLICY in binding["spec"]["policySets"]
    assert binding["spec"]["subject"] == {"namespace": ui_egress.NAMESPACE, "name": ui_egress.SERVICE_ACCOUNT}


def test_haku_anki_has_no_account_or_binding_of_its_own(resources: list[dict[str, Any]]) -> None:
    """It runs as haku-ui on purpose (actions_staging_policies.py); a separate identity would be a design change."""
    bound_subjects = {d["spec"]["subject"]["name"] for d in resources if d["kind"] == "PolicyBinding"}

    assert "haku-anki" not in bound_subjects


if __name__ == "__main__":
    pytest_bazel.main()
