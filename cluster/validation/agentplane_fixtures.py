"""In-memory agentplane cdk8s fixtures, loaded via `pytest_plugins` rather than
`conftest.py` -- importing cdk8s spawns jsii's Node subprocess at import time, and
`conftest.py` is auto-loaded for every test in this package (and its subpackages), most
of which have nothing to do with cdk8s. Test modules that need these fixtures opt in
explicitly:

    # pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot
    # see the dependency.
    # gazelle:include_dep //cluster/validation:agentplane_fixtures
    pytest_plugins = ("cluster.validation.agentplane_fixtures",)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from cdk8s import App, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s import generate_manifests

_AGENTPLANE_STAGING = "agentplane-staging"
_AGENTPLANE_TESTING = "agentplane-testing"


def _synth(chart_builder: Callable[[App], Chart]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart_builder(Cdk8sTesting.app())))


@pytest.fixture(scope="session")
def agentplane_manifests() -> dict[str, list[dict[str, Any]]]:
    """Each environment's full `agentplane.k8s.yaml` chart (Namespace/quota/RBAC, the
    model-catalog ConfigMap, and every workload), synthesized in memory -- the same
    objects `generate_manifests()` writes to disk, without the write/read round trip
    through git. See `generate_manifests.staging_chart`/`testing_chart`.
    """
    return {
        _AGENTPLANE_STAGING: _synth(generate_manifests.staging_chart),
        _AGENTPLANE_TESTING: _synth(generate_manifests.testing_chart),
    }
