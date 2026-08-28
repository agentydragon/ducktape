"""Codex app-server's provider-specific Console launch adapter.

The runner interprets Codex's stream and drives its turns at the neutral-operation generation
(#4667): the Console composes no native protocol and projects no native frames for Codex. This
adapter owns only launch -- turning the shared runtime's neutral launch facts into Codex's process
launch, with the thread/start params (model, reasoning effort, developer instructions) riding the
launch environment for the runner's CodexHarness to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from haku.console.harnesses.kind import HarnessKind
from haku.console.x.codex_app_server.config import ReasoningEffort
from haku.console.x.runtime import RuntimeLaunch
from haku.runner.codex.options import CodexAppServerSession, CodexModelProvider, HttpMcpServer, build_codex_launch
from haku.runner.protocol import HarnessLaunch


@dataclass(frozen=True, slots=True)
class CodexRuntimeAdapter:
    """Codex launch behavior, with no sandbox lifecycle state and no console projection."""

    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    model_provider: CodexModelProvider | None = None

    @property
    def kind(self) -> HarnessKind:
        return HarnessKind.CODEX_APP_SERVER

    @property
    def display_name(self) -> str:
        return "Codex"

    def build_launch(self, launch: RuntimeLaunch) -> HarnessLaunch:
        native_servers = {
            name: HttpMcpServer(url=server.url, bearer_token_env_var=server.bearer_environment_variable)
            for name, server in launch.mcp_servers.items()
        }
        return build_codex_launch(
            CodexAppServerSession(
                cwd=Path(launch.cwd),
                environment=launch.environment,
                mcp_servers=native_servers,
                model_provider=self.model_provider,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                developer_instructions=launch.appended_system_prompt,
            ),
            resume_from=launch.resume_from,
        )
