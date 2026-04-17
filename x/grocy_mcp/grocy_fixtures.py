"""Pytest fixtures that wrap the Grocy container in a `LoggedContainer` so
container stdout/stderr streams to undeclared test outputs — survives Bazel
test timeouts, unlike a plain `DockerContainer`.

For non-test bring-up (the eval CLI), import from `grocy_container` directly.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from x.grocy_mcp.grocy_container import configure_grocy_container, grocy_url, wait_for_grocy_ready


@contextmanager
def run_logged_grocy_container() -> Generator[LoggedContainer]:
    load_oci_image(GROCY)
    container = LoggedContainer(GROCY.tag, test_name="grocy")
    configure_grocy_container(container, data_dir=None)
    with container:
        wait_for_grocy_ready(container)
        yield container


@pytest.fixture(scope="session")
def grocy_container() -> Generator[LoggedContainer]:
    with run_logged_grocy_container() as container:
        yield container


@pytest.fixture(scope="session")
def grocy_base_url(grocy_container: LoggedContainer) -> str:
    return grocy_url(grocy_container)
