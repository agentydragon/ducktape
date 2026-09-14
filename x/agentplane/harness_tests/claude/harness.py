"""The pinned Claude Code binary wired to the typed Anthropic fixture."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import async_run, driver, scenarios, wire

# A routed name in the shape a LiteLLM deployment gives Claude Code; the family suffix lets it
# resolve the model's context window. The scripted upstream never dispatches on it.
MODEL = "agentplane-test/claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class ClaudeHarness:
    workspace: Path
    logs: Path
    config: Path
    binary: str
    base_environment: dict[str, str]

    def start(
        self,
        anthropic_messages: AnthropicMessages,
        *,
        resume_id: str | None = None,
        session_id: str | None = None,
        replay_user_messages: bool = False,
        hooks: bool = False,
        initialize: bool = True,
    ) -> async_run.ClaudeRun:
        # Launched as it ships: the RBE worker's glibc userland is the supported test environment.
        command = scenarios.command(
            self.binary,
            model=MODEL,
            resume_id=resume_id,
            session_id=session_id,
            replay_user_messages=replay_user_messages,
        )
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=anthropic_messages.origin, token="test-key", config_dir=str(self.config)),
        }
        # The inbound permission frame is recorded before this responder writes its approval, so
        # the run's native trace includes both sides of every fixture-injected approval.
        return async_run.ClaudeRun(
            self.logs,
            command,
            cwd=self.workspace,
            environment=environment,
            responder=_allow_permission,
            hooks=hooks,
            initialize=initialize,
        )


async def _allow_permission(frame: dict[str, Any]) -> wire.ControlResponse | None:
    match wire.parse_frame(frame):
        case wire.ControlRequestFrame(request_id=request_id, request=wire.CanUseTool(input=tool_input)):
            return driver.allow_tool(request_id, tool_input)
    return None
