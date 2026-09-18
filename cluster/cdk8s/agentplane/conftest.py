"""Each environment's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing

from cluster.cdk8s.agentplane import staging, testing
from cluster.cdk8s.agentplane.chart import environment_chart

NAMESPACES = (staging.ENV.namespace, testing.ENV.namespace)


@pytest.fixture(scope="session")
def agentplane_manifests() -> dict[str, list[dict[str, Any]]]:
    return {
        env.namespace: cast(list[dict[str, Any]], Testing.synth(environment_chart(Testing.app(), env)))
        for env in (staging.ENV, testing.ENV)
    }
