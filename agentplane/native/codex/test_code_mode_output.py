"""Keep remote command output pollable after its foreground process exits."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import replace

import pytest_bazel
from pydantic import BaseModel

from agentplane.native.codex import wire
from agentplane.native.codex.testing import model_catalog, responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses


class _CommandResult(BaseModel):
    output: str
    session_id: int | None = None
    exit_code: int | None = None


async def test_remote_output_remains_pollable_after_foreground_exit(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    openai_responses.serve_catalog([model_catalog.bundled("gpt-6-luna", slug=MODEL)])
    (codex.codex_home / "config.toml").write_text(
        "[model_providers.agentplane]\nmodel_catalog_url = "
        + json.dumps(f"{openai_responses.origin}/agentplane/codex/models")
        + "\n"
    )
    gate = codex.workspace / "output-gate"
    os.mkfifo(gate)
    # Keep a writer available for cleanup even when the assertion fails before release.
    gate_fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
    with (codex.logs / "exec-server.log").open("wb") as log:
        executor = await asyncio.create_subprocess_exec(
            codex.binary,
            "exec-server",
            "--listen",
            "ws://127.0.0.1:0",
            env=codex.base_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
        )
        try:
            assert executor.stdout is not None
            executor_url = (await executor.stdout.readline()).decode().strip()
            remote = replace(codex, base_environment={**codex.base_environment, "CODEX_EXEC_SERVER_URL": executor_url})
            async with asyncio.timeout(30), remote.start(openai_responses) as run:
                turn = await run.start_turn("Observe the remote command and collect its output.")
                # The foreground exits while a descendant holds stdout open. The test releases
                # that descendant only after observing the first result, without a scheduling sleep.
                command = f"(IFS= read -r ready < {shlex.quote(str(gate))}; printf REMOTE_OUTPUT_TAIL) &"
                async with await openai_responses.await_next_request() as exchange:
                    assert exchange.request.code_mode
                    await exchange.send(
                        *responses_sse.response_stream(
                            [
                                responses_sse.CustomToolCall(
                                    "start",
                                    "exec",
                                    f"text('RESULT ' + JSON.stringify(await tools.exec_command({json.dumps({'cmd': command, 'yield_time_ms': 1000})})));",
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                async with await openai_responses.await_next_request() as exchange:
                    (output,) = exchange.request.custom_tool_call_outputs
                    started = _CommandResult.model_validate_json(output.text.split("RESULT ", 1)[1])
                    assert started.session_id is not None, started
                    assert started.exit_code is None, started
                    assert started.output == "", started
                    os.write(gate_fd, b"release\n")
                    poll = json.dumps({"session_id": started.session_id, "chars": "", "yield_time_ms": 1000})
                    collect = (
                        f"let result = await tools.write_stdin({poll});\n"
                        "while (result.session_id != null) {\n"
                        "  const next = await tools.write_stdin({session_id: result.session_id, chars: '', yield_time_ms: 1000});\n"
                        "  result = {...next, output: result.output + next.output};\n"
                        "}\n"
                        "text('RESULT ' + JSON.stringify(result));"
                    )
                    await exchange.send(
                        *responses_sse.response_stream(
                            [responses_sse.CustomToolCall("collect", "exec", collect)], model=MODEL
                        ).events
                    )
                async with await openai_responses.await_next_request() as exchange:
                    output = exchange.request.custom_tool_call_outputs[-1]
                    completed = _CommandResult.model_validate_json(output.text.split("RESULT ", 1)[1])
                    assert completed == _CommandResult(output="REMOTE_OUTPUT_TAIL", exit_code=0)
                    await exchange.send(
                        *responses_sse.response_stream([responses_sse.Message("OUTPUT_COLLECTED")], model=MODEL).events
                    )
                assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        finally:
            os.write(gate_fd, b"release\n")
            os.close(gate_fd)
            if executor.returncode is None:
                executor.kill()
            await executor.wait()


if __name__ == "__main__":
    pytest_bazel.main()
