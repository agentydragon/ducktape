"""The pinned Codex app-server binary wired to the typed Responses fixture."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.async_process import AsyncNativeProcess
from x.agentplane.native.codex import scenarios

# Not in Codex's model catalog: a catalog model id switches Codex to code mode (one JS `exec` tool
# the model scripts against), while a routed or unknown id keeps the classic function-call shape.
MODEL = "agentplane-test-model"
EFFORT = "low"


@dataclass(frozen=True)
class CodexHarness:
    workspace: Path
    logs: Path
    codex_home: Path
    binary: str
    base_environment: dict[str, str]

    def start(self, openai_responses: OpenAIResponses) -> AsyncNativeProcess:
        endpoint = f"{openai_responses.origin}/v1"
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=endpoint, token="test-key", codex_home=str(self.codex_home)),
        }
        command = scenarios.command(self.binary, endpoint=endpoint)
        return AsyncNativeProcess(self.logs, command, cwd=self.workspace, environment=environment)
