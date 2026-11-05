import os
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from cluster.cdk8s.testing.scratch import scratch_app, scratch_app_factory, scratch_chart
from cluster.scripts.nebula_mesh_fixtures import nebula_roster


@pytest.fixture(scope="session")
def promtool(runfiles: Runfiles) -> Path:
    return runfiles.root() / os.environ["PROMTOOL_RLOCATION"]


@pytest.fixture(scope="session")
def kustomize(runfiles: Runfiles) -> Path:
    return runfiles.root() / os.environ["KUSTOMIZE_RLOCATION"]
