"""Each environment's chart synthesized in memory, for tests of the constructs' invariants."""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing

from cluster.cdk8s.agentplane import staging, testing

NAMESPACES = (staging.ENV.namespace, testing.ENV.namespace)


@pytest.fixture(scope="session")
def agentplane_manifests() -> dict[str, list[dict[str, Any]]]:
    return {
        staging.ENV.namespace: cast(list[dict[str, Any]], Testing.synth(staging.chart(Testing.app()))),
        testing.ENV.namespace: cast(list[dict[str, Any]], Testing.synth(testing.chart(Testing.app()))),
    }
