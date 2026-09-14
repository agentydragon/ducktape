"""Fixtures shared by both native agent-harness suites."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def native_logs(request: pytest.FixtureRequest) -> Path:
    """Persist each native process trace for both assertions and failed-test diagnosis."""
    logs = undeclared_outputs_dir() / "native" / str(request.node.name)
    logs.mkdir(parents=True)
    for name in ("stdin.jsonl", "stdout.jsonl", "stderr.jsonl"):
        (logs / name).touch()
    return logs


@pytest.fixture
def base_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "NO_PROXY": "127.0.0.1,localhost",
        # Native tool subprocesses inherit this deliberately minimal env.
        # Keep standard utilities available under hermetic RBE execution.
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
