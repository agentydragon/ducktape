import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
