"""Developer instructions belong to the thread, not to the resume that reloads it."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.codex import responses_sse as sse
from x.agentplane.harness_tests.codex.harness import MODEL, CodexHarness
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
from x.agentplane.native.codex import wire

STARTED_WITH = "STANDING_ORDER_ALPHA: the operator's name is Wren."
RESUMED_WITH = "STANDING_ORDER_BETA: the operator's name is Rook."
RESUMED_BASE = "You are a terse test assistant resumed with a replaced policy."


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
    async with codex.start(openai_responses, persist=True, instructions=STARTED_WITH) as first:
        turn = await first.start_turn("Reply: SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            # The instructions lead the thread, ahead of the first user message.
            assert request.item_kinds == ["message:developer", "message:user"]
            assert [message.text for message in request.messages("developer")] == [STARTED_WITH]
            stream = sse.response_stream([sse.Message("SEED_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED

    async with codex.start(
        openai_responses,
        resume_thread_id=first.thread_id,
        resume_base_instructions=RESUMED_BASE,
        resume_instructions=RESUMED_WITH,
    ) as second:
        assert second.thread_id == first.thread_id
        turn = await second.start_turn("Reply: NEXT_OK")
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
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        # Nothing on the wire reports the discarded override, so a client cannot tell it was
        # dropped: the app-server's "override was provided and ignored" notices are for a thread
        # already loaded, which a resume onto a fresh process never is.
        warnings = [frame["params"]["message"] for frame in second.native_frames() if frame.get("method") == "warning"]
        assert not [message for message in warnings if "nstructions" in message]


if __name__ == "__main__":
    pytest_bazel.main()
