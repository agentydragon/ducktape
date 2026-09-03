"""The pinned Claude Code binary wired to a scripted upstream."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.claude import driver, scenarios, wire
from x.agentplane.native.process import NativeProcess

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

    def start(self, upstream: ScriptedUpstream, *, resume_id: str | None = None) -> NativeProcess:
        # Launched as it ships: the RBE worker's glibc userland is the supported test environment.
        command = scenarios.command(self.binary, model=MODEL, resume_id=resume_id)
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=upstream.origin, token="test-key", config_dir=str(self.config)),
        }
        process = NativeProcess(self.logs, command, cwd=self.workspace, environment=environment)
        process.frame_handler = _allow_permission
        return process


def _allow_permission(frame: dict[str, Any]) -> wire.ControlResponse | None:
    match wire.parse_frame(frame):
        case wire.ControlRequestFrame(request_id=request_id, request=wire.CanUseTool(input=tool_input)):
            return driver.allow_tool(request_id, tool_input)
    return None
