"""Pytest fixture for the repository's Nebula roster."""

import pytest
from python.runfiles import Runfiles

from cluster.scripts import nebula_mesh


@pytest.fixture(scope="session")
def nebula_roster(runfiles: Runfiles) -> nebula_mesh.Mesh:
    return nebula_mesh.load(runfiles.root() / "ducktape/nebula-mesh.json")
