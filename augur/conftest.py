from __future__ import annotations

import pytest

from augur.api.config import Config, load_augur_config
from util.bazel.runfiles import get_required_path


@pytest.fixture(scope="module")
def augur_config() -> Config:
    return load_augur_config(get_required_path("_main/augur/api/testdata/config.yaml"))
