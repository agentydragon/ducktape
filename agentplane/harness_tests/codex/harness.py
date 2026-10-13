"""The pinned Codex app-server binary wired to the typed Responses fixture."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentplane.harness_tests.codex.model import EFFORT, MODEL
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.native.codex import scenarios
from agentplane.native.codex.testing import async_run
from agentplane.native.codex.testing.dynamic_tools import DynamicToolServer


@dataclass(frozen=True)
class CodexHarness:
    workspace: Path
    logs: Path
    codex_home: Path
    binary: str
    base_environment: dict[str, str]

    def start(
        self,
        openai_responses: OpenAIResponses,
        *,
        persist: bool = False,
        config: dict[str, object] | None = None,
        instructions: str = "",
        resume_thread_id: str | None = None,
        resume_base_instructions: str = "",
        resume_instructions: str = "",
        dynamic_tools: DynamicToolServer | None = None,
    ) -> async_run.CodexRun:
        endpoint = f"{openai_responses.origin}/v1"
        environment = {
            **self.base_environment,
            **scenarios.environment(endpoint=endpoint, token="test-key", codex_home=str(self.codex_home)),
        }
        command = scenarios.command(self.binary, endpoint=endpoint, max_retries=scenarios.MAX_RETRIES)
        return async_run.CodexRun(
            self.logs,
            command,
            cwd=self.workspace,
            environment=environment,
            thread_cwd=str(self.workspace),
            model=MODEL,
            effort=EFFORT,
            persist=persist,
            config=config,
            instructions=instructions,
            resume_thread_id=resume_thread_id,
            resume_base_instructions=resume_base_instructions,
            resume_instructions=resume_instructions,
            responder=dynamic_tools.respond if dynamic_tools is not None else None,
            dynamic_tools=dynamic_tools.specs() if dynamic_tools is not None else None,
        )
