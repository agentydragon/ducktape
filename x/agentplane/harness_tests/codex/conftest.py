from __future__ import annotations

from pathlib import Path

import pytest

from util.bazel.runfiles import get_required_path
from x.agentplane.harness_tests.codex.harness import CodexHarness


@pytest.fixture
def codex(workspace: Path, native_logs: Path, base_environment: dict[str, str], tmp_path: Path) -> CodexHarness:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    return CodexHarness(
        workspace=workspace,
        logs=native_logs,
        codex_home=codex_home,
        binary=str(get_required_path("agentplane_codex_cli_linux_x64/bin/codex")),
        base_environment=base_environment,
    )
