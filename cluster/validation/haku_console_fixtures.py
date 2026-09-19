"""In-memory haku console cdk8s fixtures, loaded via `pytest_plugins` for the same reason as
`agentplane_fixtures.py`: importing cdk8s spawns jsii's Node subprocess at import time, and
this package's `conftest.py` is auto-loaded for every test in it.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.directory import chart
from cluster.cdk8s.haku.charts import CONSOLE


@pytest.fixture(scope="session")
def haku_console_objects() -> list[dict[str, Any]]:
    """The `haku-console` Kustomization's chart (the console, its static shell and the
    Kubernetes API proxy), synthesized in memory -- the same objects `generate_manifests()`
    writes to `haku-console.k8s.yaml`."""
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart(Cdk8sTesting.app(), CONSOLE)))
