"""Write pytest's complete log stream to Bazel's undeclared test outputs."""

from __future__ import annotations

import pytest

from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Set the built-in log-file path before pytest's logging plugin configures itself."""
    config.option.log_file = str(undeclared_outputs_dir() / "pytest.log")
