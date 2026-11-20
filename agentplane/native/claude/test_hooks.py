"""PreToolUse hook behavior: a deny blocks the tool and the model sees why; an allow lets it run.

Registration and firing order are asserted by the native `hook_callback` control requests
themselves, not inferred from the model-visible outcome alone.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.native.claude import scenarios
from agentplane.native.claude.testing import anthropic_sse, frames
from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL

PROBE = "printf 'PROBE_OK\\n'"


@pytest.mark.parametrize(
    ("deny", "result_marker", "final_text"),
    [
        pytest.param(False, "PROBE_OK", "PROBE_DONE", id="allow"),
        pytest.param(True, scenarios.HOOK_DENIAL, "HOOK_DENIED_OBSERVED", id="deny"),
    ],
)
async def test_pretooluse_decision_gates_the_tool(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, deny: bool, result_marker: str, final_text: str
) -> None:
    async with claude.start(anthropic_messages, hooks=True, deny_tools=deny) as run:
        prompt = await run.send("Use the shell probe and report the outcome.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = anthropic_sse.message_stream(
                [anthropic_sse.ToolUse("toolu_test_1", "Bash", {"command": PROBE})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (result,) = exchange.request.tool_results
            assert result.tool_use_id == "toolu_test_1"
            assert result.is_error is deny
            assert result_marker in result.text
            stream = anthropic_sse.message_stream([anthropic_sse.Text(final_text)], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == final_text
    captured = run.native_frames()
    frames.assert_success(captured, final_text)

    pretooluse = frames.pretooluse_callbacks(captured)
    assert len(pretooluse) == 1
    assert pretooluse[0].input.get("tool_name") == "Bash"
    # A PreToolUse hook's explicit decision (allow or deny) supersedes the permission prompt
    # entirely in this pinned build: no `can_use_tool` ever follows it, either way.
    assert frames.permission_prompts(captured) == []


if __name__ == "__main__":
    pytest_bazel.main()
