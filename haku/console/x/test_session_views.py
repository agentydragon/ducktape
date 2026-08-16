"""What the conversation read model says about a session's bootstrap narration."""

from __future__ import annotations

import pytest_bazel

from haku.console.chat_models import FrameDirection
from haku.console.x.session_frames import SETUP_OUTPUT_KIND, setup_output_frame
from haku.console.x.session_runtime import SpaSession


async def test_narration_reads_back_in_the_order_the_sandbox_produced_it(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())
    for line in ("Cloning into 'haku-state'...", "done.", "Starting Claude Code."):
        await chat_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame(line)
        )

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert [line.text for line in detail.narration] == [
        "Cloning into 'haku-state'...",
        "done.",
        "Starting Claude Code.",
    ]
    assert [line.frame_seq for line in detail.narration] == sorted(line.frame_seq for line in detail.narration)


async def test_two_identical_narration_lines_are_two_lines(chat_store, operator_id) -> None:
    """The rows carry no frame identity, so nothing may collapse a repeat into a replay: a
    bootstrap that says "retrying" twice retried twice."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    for _ in range(2):
        await chat_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("retrying")
        )

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert [line.text for line in detail.narration] == ["retrying", "retrying"]
    assert len({line.frame_seq for line in detail.narration}) == 2


async def test_narration_carries_only_this_session_and_only_setup_output(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())
    other, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("mine")
    )
    await chat_store.record_frame(
        other.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("theirs")
    )
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result", "uuid": "r1"}
    )

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert [line.text for line in detail.narration] == ["mine"]


async def test_a_session_that_narrated_nothing_reports_no_narration(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert detail.narration == []


if __name__ == "__main__":
    pytest_bazel.main()
