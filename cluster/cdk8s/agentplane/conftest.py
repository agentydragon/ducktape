"""Each environment's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from cdk8s import App, Chart, Testing

from cluster.cdk8s import generate_manifests

NAMESPACES = ("agentplane-staging", "agentplane-testing")


def _synth(chart_builder: Callable[[App], Chart]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Testing.synth(chart_builder(Testing.app())))


@pytest.fixture(scope="session")
def agentplane_manifests() -> dict[str, list[dict[str, Any]]]:
    return {
        "agentplane-staging": _synth(generate_manifests.staging_chart),
        "agentplane-testing": _synth(generate_manifests.testing_chart),
    }
