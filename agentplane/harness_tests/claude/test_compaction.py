"""Native /compact, not a synthetic transcript rewrite, must retain the system prompt."""

from __future__ import annotations

import asyncio

import pytest_bazel

from agentplane.harness_tests.claude import anthropic_sse as sse
from agentplane.harness_tests.claude.harness import MODEL, ClaudeLauncher
from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.native.claude.scenarios import SYSTEM_PROMPT


async def test_manual_compact_keeps_standing_prompt_afterwards_and_on_resume(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages, hooks=True, slash_commands=True) as first:
        # /compact refuses a one-message session without ever contacting the model.
        # Build enough native conversation history to qualify for a manual compaction.
        for index in range(12):
            seed = await first.send(f"Reply SEED_{index}_OK")
            async with await anthropic_messages.await_next_request() as exchange:
                assert exchange.request.system_text.endswith(SYSTEM_PROMPT)
                await exchange.send(*sse.message_stream([sse.Text(f"SEED_{index}_OK")], model=MODEL).events)
            assert (await seed.result()).result == f"SEED_{index}_OK"

        command = await first.send("/compact")
        async with await asyncio.wait_for(anthropic_messages.await_next_request(), 45) as exchange:
            # This is the compaction summary call, NOT the request we want to assert on.
            assert exchange.request.system_text
            await exchange.send(*sse.message_stream([sse.Text("Summary: seed turn completed.")], model=MODEL).events)
        await command.result()
        assert any(
            frame.get("type") == "system" and frame.get("subtype") == "compact_boundary"
            for frame in first.native_frames()
        ), "slash command did not perform native compaction"

        prompt = await first.send("Reply AFTER_COMPACT_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.texts("user")[-1] == "Reply AFTER_COMPACT_OK"
            assert exchange.request.system_text.endswith(SYSTEM_PROMPT)
            await exchange.send(*sse.message_stream([sse.Text("AFTER_COMPACT_OK")], model=MODEL).events)
        finished = await prompt.result()
        assert finished.result == "AFTER_COMPACT_OK"

    async with claude.start(anthropic_messages, resume_id=finished.session_id) as resumed:
        prompt = await resumed.send("Reply RESUMED_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.texts("user")[-1] == "Reply RESUMED_OK"
            assert exchange.request.system_text.endswith(SYSTEM_PROMPT)
            await exchange.send(*sse.message_stream([sse.Text("RESUMED_OK")], model=MODEL).events)
        assert (await prompt.result()).result == "RESUMED_OK"


if __name__ == "__main__":
    pytest_bazel.main()
