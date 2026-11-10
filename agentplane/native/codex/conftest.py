from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.responses import OpenAIResponses


@pytest.fixture
async def openai_responses() -> AsyncGenerator[OpenAIResponses]:
    async with OpenAIResponses() as endpoint:
        yield endpoint


@pytest.fixture
def codex(workspace: Path, native_logs: Path, base_environment: dict[str, str], tmp_path: Path) -> CodexHarness:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    return CodexHarness(
        workspace=workspace,
        logs=native_logs,
        codex_home=codex_home,
        binary=str(Runfiles.CreateOrRaise().root() / "agentplane_codex_cli_linux_x64/bin/codex"),
        base_environment=base_environment,
    )
