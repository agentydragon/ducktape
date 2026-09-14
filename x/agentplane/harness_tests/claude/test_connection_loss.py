"""Upstream connection loss: native retry, the non-streaming fallback, and retry exhaustion."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.native.claude.scenarios import MAX_RETRIES


async def test_stream_lost_before_content_is_retried_without_streaming(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as run:
        prompt = await run.send("Reply with exactly: CONNECTION_RETRY_OK")

        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.stream is True
            await exchange.send(*sse.message_stream([sse.Text("lost")], model=MODEL).through("message_start").events)
            await exchange.abort()

        # Claude Code retries the same turn as a non-streaming request.
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.stream is False
            assert request.texts("user")[-1] == "Reply with exactly: CONNECTION_RETRY_OK"
            assert request.texts("assistant") == []
            await exchange.respond(sse.message_body([sse.Text("CONNECTION_RETRY_OK")], model=MODEL))

        assert (await prompt.result()).result == "CONNECTION_RETRY_OK"
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "CONNECTION_RETRY_OK")
    assert len(frames.assistant_texts(captured)) == 1


async def test_stream_lost_after_visible_text_is_retried_without_streaming(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as run:
        first = await run.send("Reply with exactly: POST_FAILURE_FIRST_OK")

        async with await anthropic_messages.await_next_request() as exchange:
            await exchange.send(
                *sse.message_stream([sse.Text("POST_FAILURE_FIRST_OK")], model=MODEL).through("text_delta").events
            )
            await exchange.abort()

        # The visible partial text is discarded and the whole turn is retried without streaming.
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.stream is False
            assert request.texts("assistant") == []
            await exchange.respond(sse.message_body([sse.Text("POST_FAILURE_FIRST_OK")], model=MODEL))
        assert (await first.result()).result == "POST_FAILURE_FIRST_OK"
        assert run.running

        second = await run.send("Reply with exactly: POST_FAILURE_FOLLOW_UP_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.texts("user")[-1] == "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
            assert request.texts("assistant") == ["POST_FAILURE_FIRST_OK"]
            stream = sse.message_stream([sse.Text("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.result()).result == "POST_FAILURE_FOLLOW_UP_OK"
    captured = run.native_frames()
    assert [terminal.is_error for terminal in frames.terminals(captured)] == [False, False]
    assert frames.assistant_texts(captured) == ["POST_FAILURE_FIRST_OK", "POST_FAILURE_FOLLOW_UP_OK"]


async def test_retry_exhaustion_fails_the_turn_and_the_process_accepts_the_next_input(
    claude: ClaudeHarness, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as run:
        first = await run.send("Reply with exactly: CONNECTION_EXHAUSTION_OK")
        for _ in range(1 + MAX_RETRIES):
            async with await anthropic_messages.await_next_request() as exchange:
                await exchange.abort()
        failed = await first.result()
        assert failed.is_error is True
        assert run.running

        second = await run.send("Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK")
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.texts("user")[-1] == "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK"
            assert request.texts("assistant") == []
            stream = sse.message_stream([sse.Text("POST_EXHAUSTION_FOLLOW_UP_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.result()).result == "POST_EXHAUSTION_FOLLOW_UP_OK"
    captured = run.native_frames()
    frames.assert_failure(frames.terminals(captured)[0], result_fragment="API Error", terminal_reason="api_error")
    assert len(frames.retry_notices(captured)) == MAX_RETRIES
    assert [terminal.is_error for terminal in frames.terminals(captured)] == [True, False]
    frames.assert_success(captured, "POST_EXHAUSTION_FOLLOW_UP_OK")


if __name__ == "__main__":
    pytest_bazel.main()
