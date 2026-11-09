"""Staging's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from cdk8s import App, Testing

from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.environment import Environment


def _synth(env: Environment, scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    # Two apps: both production chart functions name their chart "agentplane" (its manifest file).
    return cast(list[dict[str, Any]], Testing.synth(staging.chart(scratch_app_factory(), env))) + cast(
        list[dict[str, Any]], Testing.synth(staging.delivery_chart(scratch_app_factory(), env))
    )


@pytest.fixture(scope="session")
def staging_manifests(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    return _synth(staging.ENV, scratch_app_factory)


@pytest.fixture(scope="session")
def identity_catalog_manifests(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    """Staging with its policy catalog in the identity shapes, which staging does not render yet."""
    return _synth(staging.environment(identity_catalog=True), scratch_app_factory)


@pytest.fixture(scope="session", params=[False, True], ids=["serviceaccount-catalog", "identity-catalog"])
def catalog_manifests(request: pytest.FixtureRequest) -> list[dict[str, Any]]:
    """Staging under each policy catalog rendering, for invariants both must hold."""
    return cast(
        list[dict[str, Any]],
        request.getfixturevalue("identity_catalog_manifests" if request.param else "staging_manifests"),
    )
