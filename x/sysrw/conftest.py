from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio to auto mode."""
    config.option.asyncio_mode = "auto"


@pytest.fixture
def test_data_dir() -> Path:
    """Path to sysrw test data directory."""
    return Path(__file__).parent / "testdata"
