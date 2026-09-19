"""In-memory haku console cdk8s fixtures, loaded via `pytest_plugins` because importing cdk8s
spawns jsii's Node subprocess at import time, and this package's `conftest.py` is auto-loaded
for every test in it.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.haku.charts import console_chart


@pytest.fixture(scope="session")
def haku_console_objects() -> list[dict[str, Any]]:
    """The `haku-console` Kustomization's chart (the console, its static shell and the
    Kubernetes API proxy), synthesized in memory -- the same objects `generate_manifests()`
    writes to `haku-console.k8s.yaml`."""
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(console_chart(Cdk8sTesting.app())))
