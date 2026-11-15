"""Resolve the declared K3s binary for both App-backed and native suite fixtures."""

import os
from pathlib import Path

import pytest
from python.runfiles import Runfiles


@pytest.fixture(scope="session")
def k3s_binary(runfiles: Runfiles) -> Path:
    return runfiles.root() / os.environ["K3S_BINARY_RLOCATION"]
