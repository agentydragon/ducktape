"""Fixtures shared by both providers: the scripted upstream and a native process sandbox."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.process import serve


@pytest.fixture
def upstream() -> Iterator[ScriptedUpstream]:
    with serve(ScriptedUpstream()) as server:
        yield server


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def native_logs(tmp_path: Path) -> Path:
    logs = tmp_path / "native"
    logs.mkdir()
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
