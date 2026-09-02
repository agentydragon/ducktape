"""The pinned Codex app-server binary wired to a scripted upstream."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.codex import scenarios
from x.agentplane.native.process import NativeProcess

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

    def start(self, upstream: ScriptedUpstream) -> NativeProcess:
        endpoint = f"{upstream.origin}/v1"
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=endpoint, token="test-key", codex_home=str(self.codex_home)),
        }
        command = scenarios.command(self.binary, endpoint=endpoint)
        return NativeProcess(self.logs, command, cwd=self.workspace, environment=environment)
