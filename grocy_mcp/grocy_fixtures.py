"""Pytest fixtures that wrap the Grocy container in a `LoggedContainer` so
container stdout/stderr streams to undeclared test outputs — survives Bazel
test timeouts, unlike a plain `DockerContainer`.

For non-test bring-up (the eval CLI), import from `grocy_container` directly.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest
from opentelemetry import trace

from grocy_mcp.grocy_container import configure_grocy_container, grocy_custom_init_dir, grocy_url, wait_for_grocy_ready
from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer

tracer = trace.get_tracer(__name__)


@contextmanager
def run_logged_grocy_container() -> Generator[LoggedContainer]:
    load_oci_image(GROCY)
    with grocy_custom_init_dir() as init_dir:
        container = LoggedContainer(GROCY.tag, test_name="grocy")
        configure_grocy_container(container, init_dir=init_dir, data_dir=None)
        with tracer.start_as_current_span("grocy_container_start"):
            container.__enter__()
        try:
            with tracer.start_as_current_span("wait_for_grocy_ready"):
                wait_for_grocy_ready(container)
            yield container
        finally:
            container.__exit__(None, None, None)


@pytest.fixture(scope="session")
def grocy_container() -> Generator[LoggedContainer]:
    with run_logged_grocy_container() as container:
        yield container


@pytest.fixture(scope="session")
def grocy_base_url(grocy_container: LoggedContainer) -> str:
    return grocy_url(grocy_container)
