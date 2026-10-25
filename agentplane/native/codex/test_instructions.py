"""A cold resume initially replays the developer message stored in the thread history."""

from __future__ import annotations

import pytest_bazel

from agentplane.native.codex import wire
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses

STARTED_WITH = "STANDING_ORDER_ALPHA: the operator's name is Wren."
RESUMED_WITH = "STANDING_ORDER_BETA: the operator's name is Rook."
RESUMED_BASE = "You are a terse test assistant resumed with a replaced policy."


async def test_resume_replays_original_developer_message_before_compaction(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    """The first ordinary turn after resume replays the original developer message, even when
    `developerInstructions` overrides the resumed configuration. Later compaction rebuilds that
    message from the override; this probe does not exercise compaction. `baseInstructions` changes
    the base policy immediately.
    """
    async with codex.start(openai_responses, persist=True, instructions=STARTED_WITH) as first:
        turn = await first.start_turn("Reply: SEED_OK")
        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            # The instructions lead the thread, ahead of the first user message.
            assert request.item_kinds == ["message:developer", "message:user"]
            assert [message.text for message in request.messages("developer")] == [STARTED_WITH]
            stream = responses_sse.response_stream([responses_sse.Message("SEED_OK")], model=MODEL)
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
            # The base override changes the request immediately.
            assert request.instructions == RESUMED_BASE
            # One developer message, the thread's own — neither replaced nor joined by the new text.
            assert [message.text for message in request.messages("developer")] == [STARTED_WITH]
            stream = responses_sse.response_stream([responses_sse.Message("NEXT_OK")], model=MODEL)
            await exchange.send(*stream.events)
        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        # No warning says that the instruction override waits until context is rebuilt.
        warnings = [frame["params"]["message"] for frame in second.native_frames() if frame.get("method") == "warning"]
        assert not [message for message in warnings if "nstructions" in message]


if __name__ == "__main__":
    pytest_bazel.main()
