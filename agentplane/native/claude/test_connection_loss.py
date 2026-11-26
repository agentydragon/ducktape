"""Upstream connection loss: native retry, the non-streaming fallback, and retry exhaustion."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.native.claude.scenarios import MAX_RETRIES
from agentplane.native.claude.testing import anthropic_sse, frames
from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL
from agentplane.native.testing.model_endpoint import JsonResponse


@pytest.mark.parametrize("drop_after", ["message_start", "text_delta"], ids=["before-content", "after-visible-text"])
async def test_stream_loss_retries_without_streaming(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, drop_after: str
) -> None:
    prompt = "Reply with exactly: CONNECTION_RETRY_OK"
    follow_up = "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
    async with claude.start(anthropic_messages) as run:
        first = await run.send(prompt)
        async with await anthropic_messages.await_next_request() as exchange:
            assert exchange.request.stream is True
            stream = anthropic_sse.message_stream([anthropic_sse.Text("lost")], model=MODEL)
            await exchange.send(*stream.through(drop_after).events)
            await exchange.abort()

        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.stream is False
            assert request.texts("user")[-1] == prompt
            assert request.texts("assistant") == []
            await exchange.respond(anthropic_sse.message_body([anthropic_sse.Text("CONNECTION_RETRY_OK")], model=MODEL))
        assert (await first.result()).result == "CONNECTION_RETRY_OK"
        assert run.running
        frames.assert_success(run.native_frames(), "CONNECTION_RETRY_OK")

        second = await run.send(follow_up)
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.texts("user")[-1] == follow_up
            assert request.texts("assistant") == ["CONNECTION_RETRY_OK"]
            stream = anthropic_sse.message_stream([anthropic_sse.Text("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.result()).result == "POST_FAILURE_FOLLOW_UP_OK"
    captured = run.native_frames()
    assert [terminal.is_error for terminal in frames.terminals(captured)] == [False, False]
    assert frames.assistant_texts(captured) == ["CONNECTION_RETRY_OK", "POST_FAILURE_FOLLOW_UP_OK"]


@pytest.mark.parametrize("http_502", [False, True], ids=["disconnect", "http-502"])
async def test_retry_exhaustion_fails_the_turn_and_the_process_accepts_the_next_input(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, http_502: bool
) -> None:
    prompt = "Reply with exactly: FAILED_TURN"
    expected = "POST_EXHAUSTION_FOLLOW_UP_OK"
    follow_up = f"Reply with exactly: {expected}"
    async with claude.start(anthropic_messages) as run:
        first = await run.send(prompt)
        for _ in range(1 + MAX_RETRIES):
            async with await anthropic_messages.await_next_request() as exchange:
                assert exchange.request.texts("user")[-1] == prompt
                if http_502:
                    await exchange.respond(
                        JsonResponse(
                            b'{"type":"error","error":{"type":"api_error","message":"scripted upstream failure"}}',
                            status=502,
                        )
                    )
                else:
                    await exchange.abort()
        assert (await first.result()).is_error is True
        assert run.running

        second = await run.send(follow_up)
        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            assert request.texts("user")[-1] == follow_up
            if not http_502:
                assert request.texts("assistant") == []
            stream = anthropic_sse.message_stream([anthropic_sse.Text(expected)], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.result()).result == expected
    captured = run.native_frames()
    assert [terminal.is_error for terminal in frames.terminals(captured)] == [True, False]
    if not http_502:
        frames.assert_failure(frames.terminals(captured)[0], result_fragment="API Error", terminal_reason="api_error")
        assert len(frames.retry_notices(captured)) == MAX_RETRIES
        frames.assert_success(captured, expected)


if __name__ == "__main__":
    pytest_bazel.main()
