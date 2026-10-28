"""The environment a harness is started with reaches the commands it runs.

The runner gives a Session's harness the variables the `agentplane` command reads, and the model
runs that command through the harness's shell tool, so the harness has to pass them on. Codex
filters names containing `KEY`, `SECRET` or `TOKEN` from the environment it gives commands unless
configured otherwise; none of these names does.
"""

from __future__ import annotations

from dataclasses import replace

import pytest_bazel

from agentplane.native.codex import wire
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses


async def test_shell_commands_see_the_agent_cli_environment(
    codex: CodexHarness, openai_responses: OpenAIResponses, agent_cli_environment: dict[str, str]
) -> None:
    harness = replace(codex, base_environment={**codex.base_environment, **agent_cli_environment})
    async with harness.start(openai_responses) as run:
        turn = await run.start_turn("Print the agent CLI's environment.")

        async with await openai_responses.await_next_request() as exchange:
            stream = responses_sse.response_stream(
                [responses_sse.FunctionCall("call_test_1", "exec_command", {"cmd": "env | grep '^AGENTPLANE_'"})],
                model=MODEL,
            )
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (call_output,) = exchange.request.function_call_outputs
            lines = call_output.text.splitlines()
            for name, value in agent_cli_environment.items():
                assert f"{name}={value}" in lines, call_output.output
            stream = responses_sse.response_stream([responses_sse.Message("ENVIRONMENT_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
