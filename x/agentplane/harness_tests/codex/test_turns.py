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
REDISPATCH_INPUT = "Reply with exactly: CODEX_CRASH_WINDOW_REDISTPATCH"
INTERRUPTED_RESUME_INPUT = "Reply with exactly: CODEX_INTERRUPTED_RESUME_INPUT"
INTERRUPTED_RESUME_PARTIAL = "CODEX_INTERRUPTED_RESUME_PARTIAL"
INTERRUPTED_RESUME_RECOVERY = "Reply with exactly: CODEX_INTERRUPTED_RESUME_RECOVERY_OK"


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


async def test_resume_after_an_interrupted_partial_turn_keeps_the_user_item_not_partial_output(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """A fresh Codex process resumes its user item and marker, not partial assistant output.

    The interrupt has already completed before the process is killed.  This is therefore distinct
    from the active-process crash test below, and pins the typed Responses request that a runner
    sees after `thread/resume` rather than assuming interrupted stream state is durable.
    """
    async with codex.start(openai_responses, persist=True) as seeded:
        seed_turn = await seeded.start_turn("Reply with exactly: CODEX_INTERRUPTED_RESUME_SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream(
                    [sse.Reasoning("seed", "enc_interrupted_resume"), sse.Message("CODEX_INTERRUPTED_RESUME_SEED_OK")],
                    model=MODEL,
                ).events
            )
        assert (await seed_turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED

    async with codex.start(openai_responses, resume_thread_id=seeded.thread_id) as interrupted:
        turn = await interrupted.start_turn(INTERRUPTED_RESUME_INPUT)
        await turn.started()
        async with await openai_responses.await_next_request() as exchange:
            await exchange.send(
                *sse.response_stream([sse.Message(INTERRUPTED_RESUME_PARTIAL)], model=MODEL)
                .through("response.output_text.delta")
                .events
            )
            assert (await turn.agent_message_delta()).params.delta == INTERRUPTED_RESUME_PARTIAL
            assert (await interrupted.interrupt(turn)).error is None
            # Unlike an interrupt before any output, Codex reports this turn interrupted while
            # retaining the upstream Responses stream. Finish the scripted response only after
            # observing that terminal native event.
            assert (await turn.completed()).params.turn.status is wire.TurnStatus.INTERRUPTED
            await exchange.close()
        assert await interrupted.crash() < 0

    assert [
        frame.params.delta
        for frame in frames.parse(interrupted.native_frames())
        if isinstance(frame, wire.AgentMessageDelta) and frame.params.turn_id == turn.id
    ] == [INTERRUPTED_RESUME_PARTIAL]

    async with codex.start(openai_responses, resume_thread_id=seeded.thread_id) as resumed:
        recovery = await resumed.start_turn(INTERRUPTED_RESUME_RECOVERY)
        async with await openai_responses.await_next_request() as exchange:
            replay = exchange.request
            assert replay.item_kinds == [
                "message:user",
                "reasoning",
                "message:assistant",
                "message:user",
                "message:user",
                "message:user",
            ]
            assert [message.text for message in replay.messages("user")] == [
                "Reply with exactly: CODEX_INTERRUPTED_RESUME_SEED_OK",
                INTERRUPTED_RESUME_INPUT,
                frames.INTERRUPTED_TURN_MARKER,
                INTERRUPTED_RESUME_RECOVERY,
            ]
            assert [message.text for message in replay.messages("assistant")] == ["CODEX_INTERRUPTED_RESUME_SEED_OK"]
            assert replay.reasoning[0].encrypted_content == "enc_interrupted_resume"
            await exchange.send(
                *sse.response_stream([sse.Message("CODEX_INTERRUPTED_RESUME_RECOVERY_OK")], model=MODEL).events
            )
        assert (await recovery.completed()).params.turn.status is wire.TurnStatus.COMPLETED


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


async def test_resumed_redispatch_of_an_accepted_input_duplicates_native_history(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """Codex preserves accepted input but cannot correlate a replacement `turn/start` to it."""
    async with codex.start(openai_responses, persist=True) as first:
        accepted = await first.start_turn(REDISPATCH_INPUT)
        await accepted.started()
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user"]
            assert [message.text for message in request.messages("user")] == [REDISPATCH_INPUT]
            assert request.client_metadata.thread_id == first.thread_id
            assert request.client_metadata.turn_id == accepted.id
            assert await first.crash() < 0
            await exchange.wait_client_closed()
    assert not [
        frame
        for frame in frames.parse(first.native_frames())
        if isinstance(frame, wire.TurnCompleted) and frame.params.turn.id == accepted.id
    ]

    async with codex.start(openai_responses, resume_thread_id=first.thread_id) as resumed:
        assert resumed.thread_id == first.thread_id
        redispatched = await resumed.start_turn(REDISPATCH_INPUT)
        assert redispatched.id != accepted.id
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == ["message:user", "message:user"]
            assert [message.text for message in request.messages("user")] == [REDISPATCH_INPUT, REDISPATCH_INPUT]
            assert request.client_metadata.thread_id == first.thread_id
            assert request.client_metadata.turn_id == redispatched.id
            await exchange.send(
                *sse.response_stream([sse.Message("CODEX_CRASH_WINDOW_REDISTPATCH")], model=MODEL).events
            )
        assert (await redispatched.completed()).params.turn.status is wire.TurnStatus.COMPLETED


if __name__ == "__main__":
    pytest_bazel.main()
