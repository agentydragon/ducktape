"""Staging's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from cdk8s import App, Testing

from cluster.cdk8s.agentplane import runner_gateway_workload, staging


@pytest.fixture(scope="session")
def staging_manifests(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    # Two apps: both production chart functions name their chart "agentplane" (its manifest file).
    return cast(list[dict[str, Any]], Testing.synth(staging.chart(scratch_app_factory()))) + cast(
        list[dict[str, Any]],
        Testing.synth(runner_gateway_workload.serving_chart(scratch_app_factory(), env=staging.ENV)),
    )
