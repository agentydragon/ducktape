"""Staging's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing

from cluster.cdk8s.agentplane import runner_gateway_workload, staging


@pytest.fixture(scope="session")
def staging_manifests() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Testing.synth(staging.chart(Testing.app()))) + cast(
        list[dict[str, Any]], Testing.synth(runner_gateway_workload.serving_chart(Testing.app(), env=staging.ENV))
    )
