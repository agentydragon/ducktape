"""What the backfill recovers, and what it refuses to guess at.

The round trip is the test: a session driven through the write path already carries the ranges the
write path wrote, so clearing them and re-deriving them has a right answer to be measured against
rather than a literal copied into an assertion.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest_bazel
from more_itertools import one
from sqlalchemy import delete, select, update

from haku.console.chat_models import ChatMessageRole, FrameDirection, MessageUnpointable, TurnOutcome
from haku.console.database_schema import SessionMessage
from haku.console.x import message_provenance
from haku.console.x.frame_projection import projected
from haku.console.x.session_store import BridgeAuthentication, SpaSession

PROMPT = "what is in here?"


def _assistant(text: str, *, message_id: str) -> dict[str, Any]:
    """One `assistant` frame. Every frame needs its own *message_id*: `frame_uid` dedupes on it."""
    return {
        "type": "assistant",
        "message": {"id": message_id, "role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _result(text: str) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "result": text}


async def _session_through_the_write_path(chat_store, operator_id, frames: list[dict[str, Any]]) -> UUID:
    """One closed session with *frames* recorded and projected exactly as the turn loop does it."""
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id: UUID = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    prompt = await chat_store.enqueue_prompt(operator_id, session_id, PROMPT)
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    sent = await chat_store.record_frame(
        session_id, FrameDirection.TO_AGENT, "user", {"type": "user", "message": {"role": "user", "content": PROMPT}}
    )
    await chat_store.set_message_source_frames(session_id, prompt.message_id, sent.frame_seq)
    state = await chat_store.turn_state(started.turn_id)
    for payload in frames:
        recorded = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, payload["type"], payload)
        if payload["type"] != "result":
            state = await chat_store.apply_frame(
                session_id,
                started.turn_id,
                recorded.frame_seq,
                projected(frame_seq=recorded.frame_seq, payload=payload),
            )
            continue
        # `_run_turn`'s tail: a turn that completed no message of its own still writes one row,
        # and its only source is the frame that ended the turn.
        if not state.said_anything:
            message_id = await chat_store.begin_assistant(
                session_id, started.turn_id, source_first_frame_seq=recorded.frame_seq
            )
            await chat_store.update_assistant(
                session_id,
                message_id,
                str(payload["result"]),
                tool_calls=[],
                source_last_frame_seq=recorded.frame_seq,
                complete=True,
            )
        await chat_store.end_turn(started.turn_id, TurnOutcome.ANSWERED, projected_frame_seq=recorded.frame_seq)
    return session_id


async def _stored(sessions, session_id: UUID) -> list[SessionMessage]:
    async with sessions() as db:
        return list(
            (
                await db.scalars(
                    select(SessionMessage)
                    .where(SessionMessage.session_id == session_id)
                    .order_by(SessionMessage.created_at, SessionMessage.message_id)
                )
            ).all()
        )


async def _unpoint(sessions, session_id: UUID, *, role: ChatMessageRole | None = None) -> None:
    """Take the ranges away, leaving exactly the shape the pre-#4105 rows are in."""
    statement = update(SessionMessage).where(SessionMessage.session_id == session_id)
    if role is not None:
        statement = statement.where(SessionMessage.role == role)
    async with sessions() as db:
        await db.execute(statement.values(source_first_frame_seq=None, source_last_frame_seq=None))
        await db.commit()


async def _backfilled(sessions, session_id: UUID) -> message_provenance.SessionPlan:
    async with sessions() as db:
        plan = await message_provenance.plan(db, session_id)
        await message_provenance.apply(db, plan)
        await db.commit()
    return plan


async def test_the_ranges_the_write_path_wrote_are_recovered_exactly(
    chat_store, migrated_sessions, operator_id
) -> None:
    session_id = await _session_through_the_write_path(
        chat_store,
        operator_id,
        [
            _assistant("looking now", message_id="msg_1"),
            _assistant("one file", message_id="msg_2"),
            _result("one file"),
        ],
    )
    written = {
        row.message_id: (row.source_first_frame_seq, row.source_last_frame_seq)
        for row in await _stored(migrated_sessions, session_id)
    }
    await _unpoint(migrated_sessions, session_id)

    plan = await _backfilled(migrated_sessions, session_id)

    assert plan.unfillable == ()
    assert {
        row.message_id: (row.source_first_frame_seq, row.source_last_frame_seq)
        for row in await _stored(migrated_sessions, session_id)
    } == written


async def test_a_turn_whose_text_arrived_only_on_the_result_frame_points_at_that_frame(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The population the plan names: an assistant row the console synthesized rather than observed."""
    session_id = await _session_through_the_write_path(chat_store, operator_id, [_result("nothing to do")])
    assistant = one(
        [row for row in await _stored(migrated_sessions, session_id) if row.role is ChatMessageRole.ASSISTANT]
    )
    written = (assistant.source_first_frame_seq, assistant.source_last_frame_seq)
    await _unpoint(migrated_sessions, session_id, role=ChatMessageRole.ASSISTANT)

    await _backfilled(migrated_sessions, session_id)

    recovered = one(
        [row for row in await _stored(migrated_sessions, session_id) if row.role is ChatMessageRole.ASSISTANT]
    )
    assert (recovered.source_first_frame_seq, recovered.source_last_frame_seq) == written


async def test_the_prompt_is_pointed_at_the_frame_it_went_out_as(chat_store, migrated_sessions, operator_id) -> None:
    session_id = await _session_through_the_write_path(chat_store, operator_id, [_result("nothing to do")])
    prompt = one([row for row in await _stored(migrated_sessions, session_id) if row.role is ChatMessageRole.USER])
    written = (prompt.source_first_frame_seq, prompt.source_last_frame_seq)
    await _unpoint(migrated_sessions, session_id, role=ChatMessageRole.USER)

    await _backfilled(migrated_sessions, session_id)

    recovered = one([row for row in await _stored(migrated_sessions, session_id) if row.role is ChatMessageRole.USER])
    assert (recovered.source_first_frame_seq, recovered.source_last_frame_seq) == written


async def test_prose_the_frames_never_held_is_counted_rather_than_pointed_at_something(
    chat_store, migrated_sessions, operator_id
) -> None:
    """An aborted turn's notice rides on the message row and on no frame, so nothing projects to it."""
    session_id = await _session_through_the_write_path(
        chat_store, operator_id, [_assistant("half an ans", message_id="msg_1"), _result("half an ans")]
    )
    async with migrated_sessions() as db:
        await db.execute(
            update(SessionMessage)
            .where(SessionMessage.session_id == session_id, SessionMessage.role == ChatMessageRole.ASSISTANT)
            .values(content="half an ans\n\n[aborted]", source_first_frame_seq=None, source_last_frame_seq=None)
        )
        await db.commit()

    plan = await _backfilled(migrated_sessions, session_id)

    assert one(plan.unfillable).reason is MessageUnpointable.NO_MATCHING_PROJECTION
    assert (
        one(
            [row for row in await _stored(migrated_sessions, session_id) if row.role is ChatMessageRole.ASSISTANT]
        ).unpointable_reason
        is MessageUnpointable.NO_MATCHING_PROJECTION
    )


async def test_two_frames_saying_the_same_thing_and_one_row_left_is_ambiguous(
    chat_store, migrated_sessions, operator_id
) -> None:
    """Which of the two the surviving row was is exactly what nothing on it records."""
    session_id = await _session_through_the_write_path(
        chat_store,
        operator_id,
        [_assistant("done", message_id="msg_1"), _assistant("done", message_id="msg_2"), _result("done")],
    )
    rows = [row for row in await _stored(migrated_sessions, session_id) if row.role is ChatMessageRole.ASSISTANT]
    async with migrated_sessions() as db:
        await db.execute(delete(SessionMessage).where(SessionMessage.message_id == rows[0].message_id))
        await db.commit()
    await _unpoint(migrated_sessions, session_id, role=ChatMessageRole.ASSISTANT)

    plan = await _backfilled(migrated_sessions, session_id)

    assert plan.fills == ()
    assert one(plan.unfillable).reason is MessageUnpointable.AMBIGUOUS_TEXT


async def test_two_rows_and_two_identical_candidates_align_by_order(chat_store, migrated_sessions, operator_id) -> None:
    """Equal counts make the pairing arithmetic rather than a choice, so both are recoverable."""
    session_id = await _session_through_the_write_path(
        chat_store,
        operator_id,
        [_assistant("done", message_id="msg_1"), _assistant("done", message_id="msg_2"), _result("done")],
    )
    written = {
        row.message_id: (row.source_first_frame_seq, row.source_last_frame_seq)
        for row in await _stored(migrated_sessions, session_id)
    }
    await _unpoint(migrated_sessions, session_id, role=ChatMessageRole.ASSISTANT)

    plan = await _backfilled(migrated_sessions, session_id)

    assert plan.unfillable == ()
    assert {
        row.message_id: (row.source_first_frame_seq, row.source_last_frame_seq)
        for row in await _stored(migrated_sessions, session_id)
    } == written


async def test_a_plan_that_is_not_applied_writes_nothing(chat_store, migrated_sessions, operator_id) -> None:
    """The dry run is the absence of the second call, so this is what makes it one."""
    session_id = await _session_through_the_write_path(chat_store, operator_id, [_result("nothing to do")])
    await _unpoint(migrated_sessions, session_id)

    async with migrated_sessions() as db:
        plan = await message_provenance.plan(db, session_id)

    assert plan.fills != ()
    assert all(row.source_first_frame_seq is None for row in await _stored(migrated_sessions, session_id))


async def test_a_session_still_running_a_turn_is_not_offered_for_scanning(
    chat_store, migrated_sessions, operator_id
) -> None:
    """Its rows are the runtime's to point, and it is still projecting frames into them."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, PROMPT)
    assert await chat_store.next_prompt(view.session_id) is not None

    async with migrated_sessions() as db:
        assert view.session_id not in await message_provenance.unpointed_sessions(db, limit=10)


async def test_a_row_already_found_unrecoverable_is_not_scanned_again(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The reason is what makes the next run cheap: an unfillable row leaves the queue too."""
    session_id = await _session_through_the_write_path(chat_store, operator_id, [_result("nothing to do")])
    async with migrated_sessions() as db:
        await db.execute(
            update(SessionMessage)
            .where(SessionMessage.session_id == session_id, SessionMessage.role == ChatMessageRole.ASSISTANT)
            .values(content="said something else", source_first_frame_seq=None, source_last_frame_seq=None)
        )
        await db.commit()
    async with migrated_sessions() as db:
        assert session_id in await message_provenance.unpointed_sessions(db, limit=10)

    assert one((await _backfilled(migrated_sessions, session_id)).unfillable)

    async with migrated_sessions() as db:
        assert session_id not in await message_provenance.unpointed_sessions(db, limit=10)


if __name__ == "__main__":
    pytest_bazel.main()
