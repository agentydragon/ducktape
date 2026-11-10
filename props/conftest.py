"""Shared pytest configuration and tracing for Props tests."""

import os
from collections.abc import Generator

import pytest
from opentelemetry import trace

from util.testing.otel_tracing import configure_tracing


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-db",
        action="store_true",
        default=False,
        help="Preserve test database on failure for debugging (does not drop test database after test)",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode and register custom markers."""
    config.option.asyncio_mode = "auto"
    configure_tracing(config)

    # Agent DB passwords derive from PROPS_AGENT_PASSWORD_SALT, which is required
    # in production (no insecure default). Provide a fixed value for tests; set it
    # when unset OR empty so the run stays hermetic (setdefault would keep an empty
    # value and trip the fail-closed path).
    if not os.environ.get("PROPS_AGENT_PASSWORD_SALT"):
        os.environ["PROPS_AGENT_PASSWORD_SALT"] = "test-agent-password-salt"

    config.addinivalue_line("markers", "live_openai_api: marks tests calling real OpenAI API")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None]:
    """Create a root span for each test to enable hierarchical traces.

    All fixture setup/teardown and test execution runs within this span's context.
    Child spans (from timed operations like db setup, image loading, etc.) will
    automatically become children of this root span via OTel context propagation.
    """
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(f"test: {item.nodeid}"):
        yield
