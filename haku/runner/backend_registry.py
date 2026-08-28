"""Harnesses linked into the shared runner binary.

The runner itself depends only on ``Harness`` and receives this registry at its process-entry
composition boundary. Adding another harness registers another factory here; it does not add a
provider branch to the transport loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from haku.runner.backend import Harness
from haku.runner.claude.harness import ClaudeHarness, claude_harness
from haku.runner.codex.harness import CodexHarness, codex_harness

HarnessFactory = Callable[[Path | None], Harness]


def runner_harnesses() -> Mapping[str, HarnessFactory]:
    return {ClaudeHarness.name: claude_harness, CodexHarness.name: codex_harness}
