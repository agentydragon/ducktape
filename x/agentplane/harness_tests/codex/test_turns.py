"""Plain turns: one scripted exchange, and native thread resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import driver, wire

TOOLS = ["exec_command", "write_stdin", "request_user_input"]
IN_FLIGHT_INPUT = "Reply with exactly: CODEX_CRASHED_IN_FLIGHT_REPLAYED"
QUEUED_INPUT = "Reply with exactly: CODEX_CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CODEX_CRASH_RESUME_OK"


async def test_baseline_turn(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Reply with exactly: CAPTURE_BASELINE_OK")

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.model == MODEL
            assert request.stream is True
            assert request.instructions == driver.BASE_INSTRUCTIONS
            assert request.tool_names == TOOLS
            assert request.item_kinds == ["message:user"]
            assert request.messages("user")[-1].text == "Reply with exactly: CAPTURE_BASELINE_OK"
            assert request.prompt_cache_key == run.thread_id
            assert request.client_metadata.thread_id == run.thread_id
            assert request.client_metadata.turn_id == turn.id
            stream = sse.response_stream(
                [sse.Reasoning("brief", "enc_test_1"), sse.Message("CAPTURE_BASELINE_OK")], model=MODEL
            )
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "CAPTURE_BASELINE_OK")
    frames.assert_item_lifecycles(captured, wire.UserMessageItem)


async def test_idle_resume_replays_the_thread_from_disk(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses, persist=True) as first:
        turn = await first.start_turn("Reply with exactly: IDLE_RESUME_SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            stream = sse.response_stream(
                [sse.Reasoning("seed", "enc_test_1"), sse.Message("IDLE_RESUME_SEED_OK")], model=MODEL
            )
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED

    async with codex.start(openai_responses, resume_thread_id=first.thread_id) as second:
        assert second.thread_id == first.thread_id
        turn = await second.start_turn("Reply with exactly: IDLE_RESUME_OK")

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "reasoning", "message:assistant", "message:user"]
            assert [message.text for message in request.messages("user")] == [
                "Reply with exactly: IDLE_RESUME_SEED_OK",
                "Reply with exactly: IDLE_RESUME_OK",
            ]
            assert request.messages("assistant")[0].text == "IDLE_RESUME_SEED_OK"
            assert request.reasoning[0].encrypted_content == "enc_test_1"
            assert request.prompt_cache_key == first.thread_id
            stream = sse.response_stream([sse.Message("IDLE_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    frames.assert_success(second.native_frames(), "IDLE_RESUME_OK")


async def test_resume_after_crash_replays_the_in_flight_turn_but_not_its_live_followup(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """A persisted thread survives a killed app-server; its active-turn input queue does not."""
    async with codex.start(openai_responses, persist=True) as first:
        turn = await first.start_turn(IN_FLIGHT_INPUT)
        await turn.started()
        async with await openai_responses.await_next_request() as exchange:
            assert (await first.start_turn(QUEUED_INPUT)).id == turn.id
            assert await first.crash() < 0
            await exchange.wait_client_closed()

    async with codex.start(openai_responses, resume_thread_id=first.thread_id) as resumed:
        assert resumed.thread_id == first.thread_id
        recovery = await resumed.start_turn(RECOVERY_INPUT)
        async with await openai_responses.await_next_request() as exchange:
            replay = exchange.request
            assert [message.text for message in replay.messages("user")] == [IN_FLIGHT_INPUT, RECOVERY_INPUT]
            assert QUEUED_INPUT not in [message.text for message in replay.messages("user")]
            assert replay.messages("assistant") == []
            stream = sse.response_stream([sse.Message("CODEX_CRASH_RESUME_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await recovery.completed()).params.turn.status is wire.TurnStatus.COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
