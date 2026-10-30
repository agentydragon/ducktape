"""Staging's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from cdk8s import App, Testing

from cluster.cdk8s.agentplane import runner_gateway_workload, staging


@pytest.fixture(scope="session")
def staging_manifests(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    app = scratch_app_factory()
    return cast(list[dict[str, Any]], Testing.synth(staging.chart(app))) + cast(
        list[dict[str, Any]], Testing.synth(runner_gateway_workload.serving_chart(app, env=staging.ENV))
    )
