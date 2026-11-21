"""Pytest fixtures that wrap the Grocy container in a `LoggedContainer` so
container stdout/stderr streams to undeclared test outputs — survives Bazel
test timeouts, unlike a plain `DockerContainer`.

For non-test bring-up (the eval CLI), import from `grocy_container` directly.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from testcontainers.core.container import DockerContainer

from grocy_mcp.grocy_container import grocy_url, run_grocy_container
from testing.container_logs import LoggedContainer


@pytest.fixture(scope="session")
def grocy_container() -> Generator[DockerContainer]:
    with run_grocy_container(make_container=lambda image: LoggedContainer(image, test_name="grocy")) as container:
        yield container


@pytest.fixture(scope="session")
def grocy_base_url(grocy_container: DockerContainer) -> str:
    return grocy_url(grocy_container)
