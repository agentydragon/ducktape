"""The environment a harness is started with reaches the commands it runs.

The runner gives a Session's harness the variables the `agentplane` command reads, and the model
runs that command through the harness's shell tool, so the harness has to pass them on.
"""

from __future__ import annotations

from dataclasses import replace

import pytest_bazel

from agentplane.native.claude.testing import anthropic_sse
from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL


async def test_shell_commands_see_the_agent_cli_environment(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, agent_cli_environment: dict[str, str]
) -> None:
    launcher = replace(claude, base_environment={**claude.base_environment, **agent_cli_environment})
    async with launcher.start(anthropic_messages) as run:
        prompt = await run.send("Print the agent CLI's environment.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = anthropic_sse.message_stream(
                [anthropic_sse.ToolUse("toolu_test_1", "Bash", {"command": "env | grep '^AGENTPLANE_'"})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (result,) = exchange.request.tool_results
            assert result.is_error is False
            lines = result.text.splitlines()
            for name, value in agent_cli_environment.items():
                assert f"{name}={value}" in lines, result.text
            stream = anthropic_sse.message_stream([anthropic_sse.Text("ENVIRONMENT_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "ENVIRONMENT_DONE"


if __name__ == "__main__":
    pytest_bazel.main()
