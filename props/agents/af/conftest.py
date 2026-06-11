"""Enable pytest-asyncio auto mode for af tests (props/conftest.py is too heavy — it
pulls testcontainers/DB/e2e fixtures these unit tests don't need)."""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
