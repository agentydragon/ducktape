"""Developer instructions belong to the thread, not to the resume that reloads it."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.async_process import AsyncNativeProcess
from x.agentplane.native.codex import async_scenarios as scenarios, driver

STARTED_WITH = "STANDING_ORDER_ALPHA: the operator's name is Wren."
RESUMED_WITH = "STANDING_ORDER_BETA: the operator's name is Rook."
RESUMED_BASE = "You are a terse test assistant resumed with a replaced policy."


async def _next_response(process: AsyncNativeProcess, request_id: str) -> dict:
    while True:
        frame = await process.next_frame()
        if frame.get("id") == request_id:
            return frame


async def _start(process: AsyncNativeProcess, *, cwd: str, instructions: str) -> str:
    await process.send(driver.initialize("instructions-1"))
    await _next_response(process, "instructions-1")
    await process.send(driver.initialized())
    await process.send(
        driver.thread_start(
            "instructions-2", cwd=cwd, model=MODEL, effort=EFFORT, persist=True, instructions=instructions
        )
    )
    started = await _next_response(process, "instructions-2")
    return str(started["result"]["thread"]["id"])


async def _resume(process: AsyncNativeProcess, *, thread_id: str, base: str, instructions: str) -> dict:
    await process.send(driver.initialize("instructions-3"))
    await _next_response(process, "instructions-3")
    await process.send(driver.initialized())
    await process.send(
        driver.thread_resume("instructions-4", thread_id=thread_id, base_instructions=base, instructions=instructions)
    )
    return await _next_response(process, "instructions-4")


async def test_a_resume_cannot_replace_the_threads_developer_instructions(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """`thread/resume` takes a `developerInstructions` override, accepts it, and the model never
    sees it: the thread's own developer message replays out of the rollout instead, and the
    app-server says nothing about the discarded value. `baseInstructions` on the very same request
    does take effect, so the resume's overrides are live and this one is inert on its own.

    The consequence for the runner: a Codex session's standing instructions are fixed once the
    thread exists, which is why `SessionSpec.instructions` is fixed for the session's life.
    """
    async with codex.start(openai_responses) as first:
        thread_id = await _start(first, cwd=str(codex.workspace), instructions=STARTED_WITH)
        await scenarios.start_turn(first, thread_id=thread_id, request_id="instructions-5", text="Reply: SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            # The instructions lead the thread, ahead of the first user message.
            assert request.item_kinds == ["message:developer", "message:user"]
            assert [message.text for message in request.messages("developer")] == [STARTED_WITH]
            stream = sse.response_stream([sse.Message("SEED_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await scenarios.await_turn_completed(first))["params"]["turn"]["status"] == "completed"

    async with codex.start(openai_responses) as second:
        resumed = await _resume(second, thread_id=thread_id, base=RESUMED_BASE, instructions=RESUMED_WITH)
        assert "error" not in resumed
        await scenarios.start_turn(second, thread_id=thread_id, request_id="instructions-6", text="Reply: NEXT_OK")
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            # The sibling override on the same resume did land, so an inert `developerInstructions` is
            # not a resume that ignored everything.
            assert request.instructions == RESUMED_BASE
            # One developer message, the thread's own — neither replaced nor joined by the new text.
            assert [message.text for message in request.messages("developer")] == [STARTED_WITH]
            assert RESUMED_WITH not in [message.text for message in request.messages("developer")]
            stream = sse.response_stream([sse.Message("NEXT_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await scenarios.await_turn_completed(second))["params"]["turn"]["status"] == "completed"
        # Nothing on the wire reports the discarded override, so a client cannot tell it was
        # dropped: the app-server's "override was provided and ignored" notices are for a thread
        # already loaded, which a resume onto a fresh process never is.
        warnings = [frame["params"]["message"] for frame in second.stdout_frames() if frame.get("method") == "warning"]
        assert not [message for message in warnings if "nstructions" in message]


if __name__ == "__main__":
    pytest_bazel.main()
