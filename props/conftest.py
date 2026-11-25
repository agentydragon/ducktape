"""Shared pytest configuration and tracing for Props tests."""

import os

import pytest

from testing.otel_tracing import configure_tracing
from testing.pytest_otel import pytest_runtest_protocol


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--keep-db",
        action="store_true",
        default=False,
        help="Keep each test database for debugging until the PostgreSQL test container stops",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure() -> None:
    """Configure tracing and the agent password salt."""
    configure_tracing()

    # Agent DB passwords derive from PROPS_AGENT_PASSWORD_SALT, which is required
    # in production (no insecure default). Provide a fixed value for tests; set it
    # when unset OR empty so the run stays hermetic (setdefault would keep an empty
    # value and trip the fail-closed path).
    if not os.environ.get("PROPS_AGENT_PASSWORD_SALT"):
        os.environ["PROPS_AGENT_PASSWORD_SALT"] = "test-agent-password-salt"
