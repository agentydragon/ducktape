"""Claude Code's provider-specific Console launch adapter.

Sandbox claims, runner bootstrap, bridge credentials, MCP credentials and attached-chat prompt
selection are Haku infrastructure owned by ``runtime.py`` / ``session_runtime.py``.  This adapter
only translates generic launch facts into Claude's process launch; the runner interprets Claude's
own stream and projects it to neutral operations (#4667).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from haku.console.harnesses.kind import HarnessKind
from haku.console.x.runtime import RuntimeLaunch
from haku.runner.claude.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runner.protocol import HarnessLaunch


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeAdapter:
    """Claude launch behavior, with no sandbox lifecycle state and no console projection."""

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CLAUDE_CODE

    @property
    def display_name(self) -> str:
        return "Claude"

    def build_launch(self, launch: RuntimeLaunch) -> HarnessLaunch:
        session = ClaudeSession(
            append_system_prompt=launch.appended_system_prompt,
            cwd=Path(launch.cwd),
            environment=launch.environment,
            mcp_servers={
                name: HttpMcpServer(
                    url=server.url, headers={"Authorization": f"Bearer ${{{server.bearer_environment_variable}}}"}
                )
                for name, server in launch.mcp_servers.items()
            },
        )
        return build_claude_launch(session, resume_from=launch.resume_from)
