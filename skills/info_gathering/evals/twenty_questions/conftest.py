import pytest

from agent_core.testing.fixtures import *  # noqa: F403
from openai_utils.testing.fixtures import *  # noqa: F403


def pytest_configure(config: pytest.Config) -> None:
    config.option.asyncio_mode = "auto"
