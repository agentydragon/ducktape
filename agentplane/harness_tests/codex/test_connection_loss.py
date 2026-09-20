"""Upstream connection loss: native retry with its error notices, and retry exhaustion."""

from __future__ import annotations

import pytest_bazel

from agentplane.harness_tests.codex import frames, responses_sse as sse
from agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.native.codex import wire
from agentplane.native.codex.scenarios import MAX_RETRIES


async def test_stream_lost_before_content_is_retried(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Reply with exactly: CONNECTION_RETRY_OK")

        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream([sse.Message("lost")], model=MODEL).through("response.created").events
            )
            await exchange.abort()
        assert (await turn.error()).params.will_retry is True

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.stream is True
            assert request.item_kinds == ["message:user"]
            assert request.messages("user")[-1].text == "Reply with exactly: CONNECTION_RETRY_OK"
            stream = sse.response_stream([sse.Message("CONNECTION_RETRY_OK")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "CONNECTION_RETRY_OK")
    assert [error.will_retry for error in frames.errors(captured)] == [True]
    assert len(frames.agent_texts(captured)) == 1


async def test_stream_lost_after_visible_text_is_retried_and_the_thread_continues(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as run:
        first = await run.start_turn("Reply with exactly: POST_FAILURE_FIRST_OK")

        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream([sse.Message("POST_FAILURE_FIRST_OK")], model=MODEL)
                .through("response.output_text.delta")
                .events
            )
            await exchange.abort()
        assert (await first.error()).params.will_retry is True

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            # The retry resends the turn without the partial text.
            assert request.item_kinds == ["message:user"]
            stream = sse.response_stream([sse.Message("POST_FAILURE_FIRST_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await first.completed()).params.turn.status is wire.TurnStatus.COMPLETED

        second = await run.start_turn("Reply with exactly: POST_FAILURE_FOLLOW_UP_OK")
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "message:assistant", "message:user"]
            assert request.messages("assistant")[0].text == "POST_FAILURE_FIRST_OK"
            stream = sse.response_stream([sse.Message("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "POST_FAILURE_FOLLOW_UP_OK")
    assert [turn.status for turn in frames.completed_turns(captured)] == [wire.TurnStatus.COMPLETED] * 2


async def test_retry_exhaustion_fails_the_turn_and_the_thread_accepts_the_next_input(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as run:
        first = await run.start_turn("Reply with exactly: CONNECTION_EXHAUSTION_OK")
        for _ in range(1 + MAX_RETRIES):
            async with await openai_responses.await_next_request() as exchange:
                await exchange.abort()
        assert (await first.completed()).params.turn.status is wire.TurnStatus.FAILED
        assert run.running

        second = await run.start_turn("Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK")
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "message:user"]
            assert request.messages("user")[-1].text == "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK"
            stream = sse.response_stream([sse.Message("POST_EXHAUSTION_FOLLOW_UP_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    assert [error.will_retry for error in frames.errors(captured)] == [True] * MAX_RETRIES + [False]
    assert [turn.status for turn in frames.completed_turns(captured)] == [
        wire.TurnStatus.FAILED,
        wire.TurnStatus.COMPLETED,
    ]
    frames.assert_success(captured, "POST_EXHAUSTION_FOLLOW_UP_OK")


if __name__ == "__main__":
    pytest_bazel.main()
