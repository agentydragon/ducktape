"""Write pytest's complete log stream to Bazel's undeclared test outputs."""

from __future__ import annotations

import pytest

from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Route pytest's built-in file handler into the test's uploaded artifacts."""
    logging_plugin = config.pluginmanager.get_plugin("logging-plugin")
    if logging_plugin is None:
        raise RuntimeError("pytest's built-in logging plugin is unavailable")
    logging_plugin.set_log_path(str(undeclared_outputs_dir() / "pytest.log"))
