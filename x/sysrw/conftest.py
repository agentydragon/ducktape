from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def test_data_dir() -> Path:
    """Path to sysrw test data directory."""
    return Path(__file__).parent / "testdata"
