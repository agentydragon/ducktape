"""Pytest configuration for props tests.

This conftest.py imports fixtures from the testing package and exposes them
for pytest auto-discovery. Tests anywhere in props/ will have access
to these fixtures.
"""

import logging
from collections.abc import Generator

import pytest
from opentelemetry import trace

# Import fixtures from testing modules (replaces deprecated pytest_plugins)
from agent_core.testing.fixtures import *  # noqa: F403
from agent_core.testing.mcp.fixtures import *  # noqa: F403
from agent_core.testing.mcp.responses import *  # noqa: F403
from agent_core.testing.responses import *  # noqa: F403
from mcp_infra.testing.fixtures import *  # noqa: F403

# Import fixtures from our testing package for pytest discovery
# Re-export factory functions (not fixtures, but commonly used in tests)
# Testcontainers fixtures - imported directly from defining module
from props.testing.fixtures.db import (
    TEST_FIXTURES_PATH,
    db,
    engine,
    postgres_base_config,
    postgres_container,
    pytest_addoption,
    session_monkeypatch,
    synced_db,
    synced_readonly_session,
    synced_test_session,
    test_specimens_base,
)
from props.testing.fixtures.e2e import make_openai_client, mock_snapshot_slug, noop_openai_client, success_termination

# Import e2e container fixture directly from its module
from props.testing.fixtures.e2e_container import e2e_stack
from props.testing.fixtures.e2e_infra import (
    _e2e_registry_container,
    critic_dev_improve_image,
    critic_dev_optimize_image,
    critic_image,
    e2e_registry,
    e2e_registry_url,
    grader_image,
)
from props.testing.fixtures.ground_truth import (
    example_multi_tp_orm,
    example_subtract_orm,
    fp_id,
    fp_occurrence,
    fp_occurrence_id,
    get_tp_occurrences_for_snapshot,
    make_fp_occurrence,
    make_tp_occurrence,
    tp_occurrence_single,
    tp_occurrences_multi,
    tp_single_id,
    tp_single_occurrence_id,
)
from props.testing.fixtures.runs import (
    rationale_model,
    test_snapshot,
    test_train_example_with_runs,
    test_trivial_snapshot,
    test_valid_example_with_runs,
    test_validation_snapshot,
    test_validation_snapshot_slug,
)
from props.testing.fixtures.scopes import all_files_scope, subtract_file_example
from props.testing.otel_tracing import tracing


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode and register custom markers."""
    config.option.asyncio_mode = "auto"

    # Configure OTel tracing for test profiling
    tracing.configure()

    # Enable live logging by attaching pytest's log_cli_handler to root logger
    # This replicates what log_cli=true does, but programmatically
    logging_plugin = config.pluginmanager.get_plugin("logging-plugin")
    if logging_plugin and hasattr(logging_plugin, "log_cli_handler"):
        handler = logging_plugin.log_cli_handler
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter(fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
        )
        # Attach to root logger so all loggers emit to console
        root_logger = logging.getLogger()
        if handler not in root_logger.handlers:
            root_logger.addHandler(handler)
            root_logger.setLevel(logging.INFO)

    config.addinivalue_line("markers", "timeout(seconds): test timeout in seconds (requires pytest-timeout)")
    config.addinivalue_line("markers", "live_openai_api: marks tests calling real OpenAI API")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Export OTel traces at session end."""
    tracing.export_to_file()


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
