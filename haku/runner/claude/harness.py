"""Claude Code on the per-harness seam: its stdio run-loop, handshake, and native composition.

`ClaudeHarness.run` owns everything native about a Claude session — starting the CLI, the one-frame
`initialize` handshake, what a dispatched prompt and the operator's interrupt are written as,
answering the CLI's own control requests, and folding its stdout into neutral operations through
the <projection.py> `ClaudeProjector`. The runner (<../runner.py>) hands it a launch and a
`SessionApi` and starts it once; the neutral-operation numbering, journal and retention are the
session toolkit's, not this module's.

Control responses are deliberately not awaited. The v3 client correlated them to report errors to
its caller; this end has no caller to tell — the response comes back on stdout, is numbered and
recorded like every frame, and an operator reads it in the rollout. Ordering on stdin is what the
initialize handshake needs: the CLI reads its input sequentially, so a prompt written after the
initialize request is read after it, which is why the handshake is written before the command loop
starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import anyio

from haku.cli_protocol.frames import ControlRequestFrame, ControlResponse, ControlSubtype, InterruptRequest
from haku.runner.backend import (
    ProcessLaunch,
    StdinWriter,
    child_environment,
    forward_stderr,
    read_json_frames,
    shutdown,
    start_process,
)
from haku.runner.claude.options import EXECUTABLE_VARIABLE
from haku.runner.claude.projection import ClaudeProjector
from haku.runner.protocol import HarnessLaunch, Interrupt, PromptDispatch
from haku.runner.session_api import ConsoleCommand, SessionApi


@dataclass(frozen=True, slots=True)
class ClaudeHarness:
    """Claude Code, as the sandbox runner starts it, drives it, and reads it back."""

    name: ClassVar[str] = "claude"
    executable: Path

    def resolve(self, launch: HarnessLaunch) -> ProcessLaunch:
        return ProcessLaunch(
            executable=self.executable,
            arguments=launch.arguments,
            cwd=launch.cwd,
            environment=child_environment(launch),
        )

    async def run(self, launch: HarnessLaunch, session: SessionApi) -> None:
        projector = ClaudeProjector()
        process = await start_process(self.resolve(launch))
        stdout, stderr, raw_stdin = process.stdout, process.stderr, process.stdin
        assert stdout is not None
        assert stderr is not None
        assert raw_stdin is not None
        stdin = StdinWriter(raw_stdin)
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(forward_stderr, stderr, session)
                # The handshake is the first thing on stdin, before the command loop can write a
                # prompt: the CLI reads its input in order.
                await self._initialize(session, stdin)
                tasks.start_soon(self._serve_commands, session, stdin, projector)
                # Long-lived: the reader drains stdout for as long as the CLI lives, across every
                # socket, and its end is the CLI exiting — at which point nothing remains to serve.
                async for payload in read_json_frames(stdout):
                    reply = await session.observe(payload, projector.observe, self._answer_control_request)
                    if reply is not None:
                        await stdin.write_object(reply)
                await session.flushed()
                tasks.cancel_scope.cancel()
        finally:
            # Shielded: the runner cancels this harness when the console gives up, and the process
            # still has to be reaped rather than leaked.
            with anyio.CancelScope(shield=True):
                exited_with = await shutdown(process)
        if exited_with not in (0, None):
            raise RuntimeError(f"{self.name} exited with status {exited_with}")

    async def _serve_commands(self, session: SessionApi, stdin: StdinWriter, projector: ClaudeProjector) -> None:
        """Compose and inject the console's prompts and interrupts, in the order they arrive."""
        async for command in session.commands():
            payload = await self._act(session, projector, command)
            if payload is not None:
                await stdin.write_object(payload)

    async def _act(
        self, session: SessionApi, projector: ClaudeProjector, command: ConsoleCommand
    ) -> dict[str, Any] | None:
        match command:
            case PromptDispatch(prompt_id=prompt_id, text=text):
                return await session.admit(
                    prompt_id, partial(self._compose_prompt, text), partial(projector.admit, prompt_id)
                )
            case Interrupt():
                return await session.interrupt(self._compose_interrupt)

    async def _initialize(self, session: SessionApi, stdin: StdinWriter) -> None:
        """Write the handshake before anything else.

        Bare: the launch argv already carries the system prompt and MCP wiring (<options.py>),
        so the request only has to exist for the CLI to start serving.
        """
        payload = ControlRequestFrame(request={"subtype": "initialize"}).model_dump()
        await session.inject(payload)
        await stdin.write_object(payload)

    def _compose_prompt(self, text: str) -> dict[str, Any]:
        """One dispatched prompt as the CLI's native input frame.

        The `uuid` turns `command_lifecycle` reporting on and makes the prompt reachable by
        `interrupt`'s `cancel_queued`, exactly as the v3 client sent it.
        """
        return {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "uuid": str(uuid4()),
        }

    def _compose_interrupt(self) -> dict[str, Any]:
        """Abort the running turn and anything queued behind it.

        `cancel_queued` is not optional: a bare interrupt makes the CLI begin the next queued prompt
        the moment this one dies, and an operator saying "stop" does not mean "start the next thing".
        """
        return ControlRequestFrame(
            request=InterruptRequest(reason="user-cancel", cancel_queued=True).model_dump(exclude_none=True)
        ).model_dump()

    def _answer_control_request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """The refusal to write back for a CLI → runner control request, or None for other frames.

        This session registers no hooks, no `can_use_tool` and no SDK-hosted MCP server, so there is
        nothing the CLI should be asking — but it must still be answered, or it waits forever
        (measured: <../../cli_protocol/probes/harness.py>).
        """
        if payload.get("type") != "control_request":
            return None
        subtype = (payload.get("request") or {}).get("subtype")
        refusal = ControlResponse(
            subtype=ControlSubtype.ERROR,
            request_id=payload["request_id"],
            error=f"{subtype} is not supported by this runner",
        )
        return {"type": "control_response", "response": refusal.model_dump(exclude_none=True)}


def claude_harness(executable: Path | None = None) -> ClaudeHarness:
    """Claude Code at the path the sandbox image chose, or at *executable* when one is given."""
    return ClaudeHarness(
        executable=executable if executable is not None else Path(os.environ.get(EXECUTABLE_VARIABLE, "claude"))
    )
