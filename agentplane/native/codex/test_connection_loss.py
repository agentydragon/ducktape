"""Upstream connection loss: native retry with its error notices, and retry exhaustion."""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.native.codex import scenarios, wire
from agentplane.native.codex.scenarios import MAX_RETRIES
from agentplane.native.codex.testing import frames, responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.model_endpoint import JsonResponse


@pytest.mark.parametrize(
    "drop_after", ["response.created", "response.output_text.delta"], ids=["before-content", "after-visible-text"]
)
async def test_stream_loss_retries_and_the_thread_continues(
    codex: CodexHarness, openai_responses: OpenAIResponses, drop_after: str
) -> None:
    prompt = "Reply with exactly: CONNECTION_RETRY_OK"
    follow_up = "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
    async with codex.start(openai_responses) as run:
        first = await run.start_turn(prompt)
        async with await openai_responses.await_next_request() as exchange:
            stream = responses_sse.response_stream([responses_sse.Message("lost")], model=MODEL)
            await exchange.send(*stream.through(drop_after).events)
            await exchange.abort()
        assert (await first.error()).params.will_retry is True

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.stream is True
            assert request.item_kinds == ["message:developer", "message:user"]
            assert [message.text for message in request.messages("developer")] == [scenarios.BASE_INSTRUCTIONS]
            assert request.messages("user")[-1].text == prompt
            stream = responses_sse.response_stream([responses_sse.Message("CONNECTION_RETRY_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await first.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running

        second = await run.start_turn(follow_up)
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:developer", "message:user", "message:assistant", "message:user"]
            assert request.messages("assistant")[0].text == "CONNECTION_RETRY_OK"
            stream = responses_sse.response_stream([responses_sse.Message("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "POST_FAILURE_FOLLOW_UP_OK")
    assert [error.will_retry for error in frames.errors(captured)] == [True]
    assert frames.agent_texts(captured) == ["CONNECTION_RETRY_OK", "POST_FAILURE_FOLLOW_UP_OK"]
    assert [turn.status for turn in frames.completed_turns(captured)] == [wire.TurnStatus.COMPLETED] * 2


@pytest.mark.parametrize("http_502", [False, True], ids=["disconnect", "http-502"])
async def test_retry_exhaustion_fails_the_turn_and_the_thread_accepts_the_next_input(
    codex: CodexHarness, openai_responses: OpenAIResponses, http_502: bool
) -> None:
    prompt = "Reply with exactly: FAILED_TURN"
    expected = "POST_EXHAUSTION_FOLLOW_UP_OK"
    follow_up = f"Reply with exactly: {expected}"
    async with codex.start(openai_responses) as run:
        first = await run.start_turn(prompt)
        for attempt in range(1 + MAX_RETRIES):
            async with await openai_responses.await_next_request() as exchange:
                assert exchange.request.messages("user")[-1].text == prompt
                if http_502:
                    await exchange.respond(
                        JsonResponse(
                            b'{"error":{"type":"server_error","message":"scripted upstream failure"}}', status=502
                        )
                    )
                else:
                    await exchange.abort()
            if http_502 and attempt < MAX_RETRIES:
                assert (await first.error()).params.will_retry
        assert (await first.completed()).params.turn.status is wire.TurnStatus.FAILED
        assert run.running

        second = await run.start_turn(follow_up)
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            if not http_502:
                assert request.item_kinds == ["message:developer", "message:user", "message:user"]
            assert request.messages("user")[-1].text == follow_up
            stream = responses_sse.response_stream([responses_sse.Message(expected)], model=MODEL)
            await exchange.send(*stream.events)
        assert (await second.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    assert [turn.status for turn in frames.completed_turns(captured)] == [
        wire.TurnStatus.FAILED,
        wire.TurnStatus.COMPLETED,
    ]
    if not http_502:
        assert [error.will_retry for error in frames.errors(captured)] == [True] * MAX_RETRIES + [False]
        frames.assert_success(captured, expected)


if __name__ == "__main__":
    pytest_bazel.main()
