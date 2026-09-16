"""PreToolUse hook behavior: a deny blocks the tool and the model sees why; an allow lets it run.

Registration and firing order are asserted by the native `hook_callback` control requests
themselves, not inferred from the model-visible outcome alone.
"""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude import scenarios

PROBE = "printf 'PROBE_OK\\n'"


async def test_pretooluse_allow_lets_the_tool_run(claude: ClaudeHarness, anthropic_messages: AnthropicMessages) -> None:
    async with claude.start(anthropic_messages, hooks=True) as run:
        prompt = await run.send("Use the shell probe and report PROBE_DONE.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.ToolUse("toolu_test_1", "Bash", {"command": PROBE})], model=MODEL)
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (result,) = exchange.request.tool_results
            assert result.tool_use_id == "toolu_test_1"
            assert result.is_error is False
            assert "PROBE_OK" in result.text
            stream = sse.message_stream([sse.Text("PROBE_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "PROBE_DONE"
    captured = run.native_frames()
    frames.assert_success(captured, "PROBE_DONE")

    pretooluse = frames.pretooluse_callbacks(captured)
    assert len(pretooluse) == 1
    assert pretooluse[0].input.get("tool_name") == "Bash"
    # A PreToolUse hook's explicit decision (allow or deny) supersedes the permission prompt
    # entirely in this pinned build: no `can_use_tool` ever follows it, either way.
    assert frames.permission_prompts(captured) == []


async def test_pretooluse_deny_blocks_the_tool_and_the_model_sees_why(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages, hooks=True, deny_tools=True) as run:
        prompt = await run.send("Use the shell probe and report the outcome.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = sse.message_stream([sse.ToolUse("toolu_test_1", "Bash", {"command": PROBE})], model=MODEL)
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (result,) = exchange.request.tool_results
            assert result.tool_use_id == "toolu_test_1"
            assert result.is_error is True
            assert scenarios.HOOK_DENIAL in result.text
            stream = sse.message_stream([sse.Text("HOOK_DENIED_OBSERVED")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "HOOK_DENIED_OBSERVED"
    captured = run.native_frames()
    frames.assert_success(captured, "HOOK_DENIED_OBSERVED")

    pretooluse = frames.pretooluse_callbacks(captured)
    assert len(pretooluse) == 1
    assert pretooluse[0].input.get("tool_name") == "Bash"
    assert frames.permission_prompts(captured) == []


if __name__ == "__main__":
    pytest_bazel.main()
