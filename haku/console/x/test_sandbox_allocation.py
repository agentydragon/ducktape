"""The one rule that buys sandboxes, and the property it exists for: every surface obeys it.

**No channel is imported here.** A room reaches these tests as the `room_id` a `MatrixSession`
records and nothing else, which is the point — what the sweep reads is a session row and an
unclaimed prompt, and it cannot tell which surface wrote either.
"""

from __future__ import annotations

import pytest_bazel

from haku.console.chat_models import SessionStatus
from haku.console.x.session_store import MatrixSession, SpaSession

ROOM = "!room:example.org"


async def test_a_session_nobody_has_spoken_to_keeps_its_idle_row(
    allocator, chat_service, chat_store, recording_claims, operator_id
) -> None:
    """The standing cost the idle state removes: a conversation opened and left alone."""
    view = await chat_service.create(operator_id, SpaSession())

    await allocator.allocate_once()

    assert recording_claims.created == []
    assert await chat_store.status(view.session_id) == SessionStatus.IDLE


async def test_a_conversation_the_browser_opened_gets_its_sandbox_from_the_first_prompt(
    allocator, chat_service, chat_store, recording_claims, operator_id
) -> None:
    """The SPA's whole path, and the asymmetry this replaced: `POST /api/conversations` used to
    allocate in the request, so the browser never saw an idle session and the room's rule was
    Matrix's alone. Both now wait for the same thing (<README.md> § An idle session)."""
    conversation = await chat_service.create_conversation(operator_id)
    session_id = conversation.session.session_id
    assert recording_claims.created == []

    await chat_store.enqueue_prompt(operator_id, session_id, "are you there?")
    await allocator.allocate_once()

    assert recording_claims.created == [session_id]
    assert await chat_store.status(session_id) == SessionStatus.PROVISIONING


async def test_one_pass_treats_a_room_s_session_and_a_browser_s_alike(
    allocator, chat_service, chat_store, recording_claims, operator_id
) -> None:
    """The test that would have caught the asymmetry: two sessions differing only in surface, one
    prompt each, one sweep — and the sweep has no way to tell them apart."""
    spa = await chat_service.create(operator_id, SpaSession())
    room = await chat_service.create(operator_id, MatrixSession(room_id=ROOM))
    for session_id in (spa.session_id, room.session_id):
        await chat_store.enqueue_prompt(operator_id, session_id, "are you there?")

    await allocator.allocate_once()

    assert sorted(recording_claims.created) == sorted([spa.session_id, room.session_id])


async def test_a_second_pass_does_not_make_a_second_claim(
    allocator, chat_service, chat_store, recording_claims, operator_id
) -> None:
    """A sweep runs every interval and on every prompt notification, so re-reading a session it has
    already served must be a no-op rather than a second sandbox."""
    view = await chat_service.create(operator_id, SpaSession())
    await chat_store.enqueue_prompt(operator_id, view.session_id, "are you there?")

    await allocator.allocate_once()
    await allocator.allocate_once()

    assert recording_claims.created == [view.session_id]


async def test_a_session_whose_sandbox_could_not_be_created_does_not_stop_the_pass(
    allocator, chat_service, chat_store, recording_claims, operator_id
) -> None:
    """Kubernetes refusing one claim must not leave every other waiting session unserved — and the
    session it refused says so on its own row, which is what a later pass reads."""
    refused = await chat_service.create(operator_id, SpaSession())
    served = await chat_service.create(operator_id, SpaSession())
    for session_id in (refused.session_id, served.session_id):
        await chat_store.enqueue_prompt(operator_id, session_id, "are you there?")
    recording_claims.refuse(refused.session_id)

    await allocator.allocate_once()

    assert recording_claims.created == [served.session_id]
    assert await chat_store.status(refused.session_id) == SessionStatus.FAILED


if __name__ == "__main__":
    pytest_bazel.main()
