"""Staging's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from cdk8s import App, Testing

from cluster.cdk8s.agentplane import runner_gateway_workload, staging


@pytest.fixture(scope="session")
def staging_manifests(scratch_app: Callable[[], App]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Testing.synth(staging.chart(scratch_app()))) + cast(
        list[dict[str, Any]], Testing.synth(runner_gateway_workload.serving_chart(scratch_app(), env=staging.ENV))
    )
