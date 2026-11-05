from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages


@pytest.fixture
async def anthropic_messages() -> AsyncGenerator[AnthropicMessages]:
    async with AnthropicMessages() as endpoint:
        yield endpoint


@pytest.fixture
def claude(workspace: Path, native_logs: Path, base_environment: dict[str, str], tmp_path: Path) -> ClaudeLauncher:
    config = tmp_path / ".claude"
    config.mkdir()
    return ClaudeLauncher(
        workspace=workspace,
        logs=native_logs,
        config=config,
        binary=str(Runfiles.CreateOrRaise().root() / "claude_code_cli_linux_x64/claude"),
        base_environment=base_environment,
    )
