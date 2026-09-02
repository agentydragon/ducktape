from __future__ import annotations

from pathlib import Path

import pytest

from util.bazel.runfiles import get_required_path
from x.agentplane.harness_tests.claude.harness import ClaudeHarness


@pytest.fixture
def claude(workspace: Path, native_logs: Path, base_environment: dict[str, str], tmp_path: Path) -> ClaudeHarness:
    config = tmp_path / ".claude"
    config.mkdir()
    return ClaudeHarness(
        workspace=workspace,
        logs=native_logs,
        config=config,
        binary=str(get_required_path("claude_code_cli_linux_x64/claude")),
        base_environment=base_environment,
    )
