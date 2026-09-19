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

from typing import Any, cast

import pytest
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.agentplane import staging, testing


@pytest.fixture(scope="session")
def agentplane_manifests() -> dict[str, list[dict[str, Any]]]:
    """Each environment's full `agentplane.k8s.yaml` chart (Namespace/quota/RBAC, the
    model-catalog ConfigMap, and every workload), synthesized in memory -- the same
    objects `generate_manifests()` writes to disk, without the write/read round trip
    through git.
    """
    return {
        staging.ENV.namespace: cast(list[dict[str, Any]], Cdk8sTesting.synth(staging.chart(Cdk8sTesting.app()))),
        testing.ENV.namespace: cast(list[dict[str, Any]], Cdk8sTesting.synth(testing.chart(Cdk8sTesting.app()))),
    }
