from __future__ import annotations

import pytest

from augur.api.config import Config
from augur.api.testing import load_fixture_config


@pytest.fixture(scope="module")
def augur_config() -> Config:
    return load_fixture_config()
