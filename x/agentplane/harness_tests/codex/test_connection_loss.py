"""Upstream connection loss: native retry with its error notices, and retry exhaustion."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import async_scenarios as scenarios, wire
from x.agentplane.native.codex.scenarios import MAX_RETRIES


async def test_stream_lost_before_content_is_retried(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as process:
        thread_id = (await scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT))[
            "thread_id"
        ]
        await scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: CONNECTION_RETRY_OK"
        )

        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream([sse.Message("lost")], model=MODEL).through("response.created").events
            )
            await exchange.abort()
        notice = await scenarios.await_error(process)
        assert notice["params"]["willRetry"] is True

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.stream is True
            assert request.item_kinds == ["message:user"]
            assert request.messages("user")[-1].text == "Reply with exactly: CONNECTION_RETRY_OK"
            stream = sse.response_stream([sse.Message("CONNECTION_RETRY_OK")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await scenarios.await_turn_completed(process))["params"]["turn"]["status"] == "completed"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "CONNECTION_RETRY_OK")
    assert [error.will_retry for error in frames.errors(captured)] == [True]
    assert len(frames.agent_texts(captured)) == 1


async def test_stream_lost_after_visible_text_is_retried_and_the_thread_continues(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as process:
        thread_id = (await scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT))[
            "thread_id"
        ]
        await scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: POST_FAILURE_FIRST_OK"
        )

        exchange = await openai_responses.await_next_request()
        await exchange.send(
            *sse.response_stream([sse.Message("POST_FAILURE_FIRST_OK")], model=MODEL)
            .through("response.output_text.delta")
            .events
        )
        await exchange.abort()
        notice = await scenarios.await_error(process)
        assert notice["params"]["willRetry"] is True

        exchange = await openai_responses.await_next_request()
        request = exchange.request
        # The retry resends the turn without the partial text.
        assert request.item_kinds == ["message:user"]
        stream = sse.response_stream([sse.Message("POST_FAILURE_FIRST_OK")], model=MODEL)
        await exchange.send(*stream.events)
        await exchange.close()
        assert (await scenarios.await_turn_completed(process))["params"]["turn"]["status"] == "completed"

        await scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-4", text="Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
        )
        exchange = await openai_responses.await_next_request()
        request = exchange.request
        assert request.item_kinds == ["message:user", "message:assistant", "message:user"]
        assert request.messages("assistant")[0].text == "POST_FAILURE_FIRST_OK"
        stream = sse.response_stream([sse.Message("POST_FAILURE_FOLLOW_UP_OK")], model=MODEL)
        await exchange.send(*stream.events)
        await exchange.close()
        assert (await scenarios.await_turn_completed(process))["params"]["turn"]["status"] == "completed"
    captured = process.stdout_frames()
    frames.assert_success(captured, "POST_FAILURE_FOLLOW_UP_OK")
    assert [turn.status for turn in frames.completed_turns(captured)] == [wire.TurnStatus.COMPLETED] * 2


async def test_retry_exhaustion_fails_the_turn_and_the_thread_accepts_the_next_input(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as process:
        thread_id = (await scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT))[
            "thread_id"
        ]
        await scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: CONNECTION_EXHAUSTION_OK"
        )
        for _ in range(1 + MAX_RETRIES):
            await (await openai_responses.await_next_request()).abort()
        failed = await scenarios.await_turn_completed(process)
        assert failed["params"]["turn"]["status"] == "failed"
        assert process.alive()

        await scenarios.start_turn(
            process,
            thread_id=thread_id,
            request_id="capture-4",
            text="Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK",
        )
        exchange = await openai_responses.await_next_request()
        request = exchange.request
        assert request.item_kinds == ["message:user", "message:user"]
        assert request.messages("user")[-1].text == "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK"
        stream = sse.response_stream([sse.Message("POST_EXHAUSTION_FOLLOW_UP_OK")], model=MODEL)
        await exchange.send(*stream.events)
        await exchange.close()
        assert (await scenarios.await_turn_completed(process))["params"]["turn"]["status"] == "completed"
    captured = process.stdout_frames()
    assert [error.will_retry for error in frames.errors(captured)] == [True] * MAX_RETRIES + [False]
    assert [turn.status for turn in frames.completed_turns(captured)] == [
        wire.TurnStatus.FAILED,
        wire.TurnStatus.COMPLETED,
    ]
    frames.assert_success(captured, "POST_EXHAUSTION_FOLLOW_UP_OK")


if __name__ == "__main__":
    pytest_bazel.main()
