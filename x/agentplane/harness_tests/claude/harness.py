"""The pinned Claude Code binary wired to the typed Anthropic fixture."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import async_run, driver, mcp, scenarios, wire

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
        deny_tools: bool = False,
        driver_tools: mcp.DriverMcpServer | None = None,
        initialize: bool = True,
    ) -> async_run.ClaudeRun:
        # Launched as it ships: the RBE worker's glibc userland is the supported test environment.
        # `hooks` drops `--safe-mode` (which would otherwise disable them) for `--include-hook-events`.
        command = scenarios.command(
            self.binary,
            model=MODEL,
            resume_id=resume_id,
            session_id=session_id,
            replay_user_messages=replay_user_messages,
            hooks=hooks,
        )
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=anthropic_messages.origin, token="test-key", config_dir=str(self.config)),
        }
        # The inbound permission frame is recorded before this responder writes its approval, so
        # the run's native trace includes both sides of every fixture-injected approval.
        responder = _hook_responder(deny_tools=deny_tools) if hooks else _allow_permission
        if driver_tools is not None:
            responder = _compose(driver_tools.respond, responder)
        return async_run.ClaudeRun(
            self.logs,
            command,
            cwd=self.workspace,
            environment=environment,
            responder=responder,
            hooks=hooks,
            sdk_mcp_servers=[driver_tools.name] if driver_tools is not None else None,
            initialize=initialize,
        )


async def _allow_permission(frame: dict[str, Any]) -> wire.ControlResponse | None:
    match wire.parse_frame(frame):
        case wire.ControlRequestFrame(request_id=request_id, request=wire.CanUseTool(input=tool_input)):
            return driver.allow_tool(request_id, tool_input)
    return None


def _hook_responder(*, deny_tools: bool) -> Callable[[dict[str, Any]], Awaitable[wire.ControlResponse | None]]:
    """Answers every `PreToolUse` hook callback with allow or deny, and any resulting `can_use_tool`
    permission prompt with allow, exactly as `scenarios.hook_answers` computes synchronously."""
    answer = scenarios.hook_answers(deny_tools=deny_tools)

    async def responder(frame: dict[str, Any]) -> wire.ControlResponse | None:
        return answer(frame)

    return responder


def _compose(
    *responders: Callable[[dict[str, Any]], Awaitable[wire.ControlResponse | None]],
) -> Callable[[dict[str, Any]], Awaitable[wire.ControlResponse | None]]:
    """Tries each responder in order; the first non-`None` answer wins."""

    async def composed(frame: dict[str, Any]) -> wire.ControlResponse | None:
        for responder in responders:
            reply = await responder(frame)
            if reply is not None:
                return reply
        return None

    return composed
