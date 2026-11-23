"""Import the runtest hook in a conftest to trace each case from setup through teardown."""

from collections.abc import Generator

import pytest
from opentelemetry import trace


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None]:
    with trace.get_tracer(__name__).start_as_current_span(f"test: {item.nodeid}"):
        yield
