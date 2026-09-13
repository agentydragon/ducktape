"""Plain turns: one scripted exchange, and native thread resume across processes."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import async_scenarios as scenarios, driver, wire

TOOLS = ["exec_command", "write_stdin", "request_user_input"]
IN_FLIGHT_INPUT = "Reply with exactly: CODEX_CRASHED_IN_FLIGHT_REPLAYED"
QUEUED_INPUT = "Reply with exactly: CODEX_CRASHED_QUEUE_FATE"
RECOVERY_INPUT = "Reply with exactly: CODEX_CRASH_RESUME_OK"


async def test_baseline_turn(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as process:
        thread_id = (await scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT))[
            "thread_id"
        ]
        turn_id = await scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: CAPTURE_BASELINE_OK"
        )

        exchange = await openai_responses.await_next_request()
        request = exchange.request
        assert request.model == MODEL
        assert request.stream is True
        assert request.instructions == driver.BASE_INSTRUCTIONS
        assert request.tool_names == TOOLS
        assert request.item_kinds == ["message:user"]
        assert request.messages("user")[-1].text == "Reply with exactly: CAPTURE_BASELINE_OK"
        assert request.prompt_cache_key == thread_id
        assert request.client_metadata.thread_id == thread_id
        assert request.client_metadata.turn_id == turn_id
        stream = sse.response_stream(
            [sse.Reasoning("brief", "enc_test_1"), sse.Message("CAPTURE_BASELINE_OK")], model=MODEL
        )
        await exchange.send(*stream.events)
        await exchange.close()

        terminal = await scenarios.await_turn_completed(process)
        assert terminal["params"]["turn"]["status"] == "completed"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "CAPTURE_BASELINE_OK")
    frames.assert_item_lifecycles(captured, wire.UserMessageItem)


async def test_idle_resume_replays_the_thread_from_disk(codex: CodexHarness, openai_responses: OpenAIResponses) -> None:
    async with codex.start(openai_responses) as first:
        thread_id = (
            await scenarios.launch_handshake(first, cwd=str(codex.workspace), model=MODEL, effort=EFFORT, persist=True)
        )["thread_id"]
        await scenarios.start_turn(
            first, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: IDLE_RESUME_SEED_OK"
        )
        exchange = await openai_responses.await_next_request()
        stream = sse.response_stream(
            [sse.Reasoning("seed", "enc_test_1"), sse.Message("IDLE_RESUME_SEED_OK")], model=MODEL
        )
        await exchange.send(*stream.events)
        await exchange.close()
        assert (await scenarios.await_turn_completed(first))["params"]["turn"]["status"] == "completed"

    async with codex.start(openai_responses) as second:
        resumed = await scenarios.resume_handshake(second, thread_id=thread_id)
        assert resumed["thread_resume_response"]["result"]["thread"]["id"] == thread_id
        await scenarios.start_turn(
            second, thread_id=thread_id, request_id="capture-6", text="Reply with exactly: IDLE_RESUME_OK"
        )

        exchange = await openai_responses.await_next_request()
        request = exchange.request
        assert request.item_kinds == ["message:user", "reasoning", "message:assistant", "message:user"]
        assert [message.text for message in request.messages("user")] == [
            "Reply with exactly: IDLE_RESUME_SEED_OK",
            "Reply with exactly: IDLE_RESUME_OK",
        ]
        assert request.messages("assistant")[0].text == "IDLE_RESUME_SEED_OK"
        assert request.reasoning[0].encrypted_content == "enc_test_1"
        assert request.prompt_cache_key == thread_id
        stream = sse.response_stream([sse.Message("IDLE_RESUME_OK")], model=MODEL)
        await exchange.send(*stream.events)
        await exchange.close()
        assert (await scenarios.await_turn_completed(second))["params"]["turn"]["status"] == "completed"
    frames.assert_success(second.stdout_frames(), "IDLE_RESUME_OK")


async def test_resume_after_crash_replays_the_in_flight_turn_but_not_its_live_followup(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """A persisted thread survives a killed app-server; its active-turn input queue does not."""
    async with codex.start(openai_responses) as first:
        thread_id = (
            await scenarios.launch_handshake(first, cwd=str(codex.workspace), model=MODEL, effort=EFFORT, persist=True)
        )["thread_id"]
        turn_id = await scenarios.start_turn(first, thread_id=thread_id, request_id="crash-3", text=IN_FLIGHT_INPUT)
        await scenarios.await_turn_started(first)
        exchange = await openai_responses.await_next_request()

        await first.send(driver.turn_start("crash-4", thread_id=thread_id, text=QUEUED_INPUT))
        while True:
            joined = await first.next_frame()
            if joined.get("id") == "crash-4":
                break
        assert joined["result"]["turn"]["id"] == turn_id
        assert await first.crash() < 0
    await exchange.wait_client_closed()

    async with codex.start(openai_responses) as resumed:
        assert (await scenarios.resume_handshake(resumed, thread_id=thread_id))["thread_id"] == thread_id
        await scenarios.start_turn(resumed, thread_id=thread_id, request_id="crash-6", text=RECOVERY_INPUT)
        exchange = await openai_responses.await_next_request()
        replay = exchange.request
        assert [message.text for message in replay.messages("user")] == [IN_FLIGHT_INPUT, RECOVERY_INPUT]
        assert QUEUED_INPUT not in [message.text for message in replay.messages("user")]
        assert replay.messages("assistant") == []
        stream = sse.response_stream([sse.Message("CODEX_CRASH_RESUME_OK")], model=MODEL)
        await exchange.send(*stream.events)
        await exchange.close()
        assert (await scenarios.await_turn_completed(resumed))["params"]["turn"]["status"] == "completed"


if __name__ == "__main__":
    pytest_bazel.main()
