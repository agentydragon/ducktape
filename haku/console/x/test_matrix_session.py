"""What the supervisor does when the room's session is missing, live, or dead."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import delete

from haku.console.database_schema import ClaudeChatSession
from haku.console.x.chat_notifications import ChatNotifications
from haku.console.x.claude_chat import ClaudeChatService, ClaudeChatStore
from haku.console.x.conftest import MATRIX_CONFIG, MATRIX_ROOM, MATRIX_USER
from haku.console.x.matrix_session import MatrixConversationStore, MatrixSessionSupervisor


async def bound_session(conversations: MatrixConversationStore) -> UUID | None:
    conversation = await conversations.load(MATRIX_USER)
    assert conversation is not None
    return conversation.session_id


@pytest.fixture
def announced() -> list[str]:
    """What the supervisor said into the room."""
    return []


@pytest.fixture
def supervisor(
    conversations: MatrixConversationStore,
    chat_store: ClaudeChatStore,
    chat_service: ClaudeChatService,
    notifications: ChatNotifications,
    migrated_identity_store,
    announced: list[str],
) -> MatrixSessionSupervisor:
    """The supervisor over real stores, with only Kubernetes and the announce sink stood in."""

    async def _announce(body: str) -> None:
        announced.append(body)

    return MatrixSessionSupervisor(
        MATRIX_CONFIG,
        conversations,
        chat_service,
        chat_store,
        notifications,
        migrated_identity_store,
        _announce,
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive `supervise_once`
    )


async def test_does_nothing_before_a_room_is_bound(supervisor, recording_claims, announced) -> None:
    """Nothing to serve, and nowhere to say so — provisioning here would be a sandbox nobody can reach."""

    await supervisor.supervise_once()

    assert (recording_claims.created, announced) == ([], [])


async def test_provisions_a_session_for_a_freshly_bound_room(
    supervisor, conversations, chat_store, recording_claims, announced
) -> None:
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)

    await supervisor.supervise_once()

    [session_id] = recording_claims.created
    assert await bound_session(conversations) == session_id
    assert await chat_store.status(session_id) == "provisioning"
    assert "provisioning a sandbox" in announced[0]


async def test_leaves_a_live_session_alone(supervisor, conversations, recording_claims) -> None:
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [live] = recording_claims.created

    await supervisor.supervise_once()

    assert recording_claims.created == [live], "a live session was replaced"


async def test_replaces_a_failed_session(supervisor, conversations, chat_store, recording_claims, announced) -> None:
    """A dead session over Matrix is invisible — the room would just stop answering."""
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [dead] = recording_claims.created
    await chat_store.fail(dead, "the sandbox went away")

    await supervisor.supervise_once()

    assert len(recording_claims.created) == 2
    assert await bound_session(conversations) not in (None, dead)
    assert dead in recording_claims.deleted, "the dead session's claim must be swept before a new one is made"
    assert any("ended" in line for line in announced)


async def test_replaces_a_session_whose_row_is_gone(
    supervisor, conversations, recording_claims, migrated_sessions
) -> None:
    """A deleted session leaves the room bound but unserved, and the next pass re-provisions.

    The fake store this replaced pointed the binding at a session id that had never existed.
    Postgres refuses that: `matrix_conversation.session_id` is a foreign key, so a bound
    session always references a real row. What the schema does allow — and what this now
    covers — is the row being deleted underneath the binding, which `ondelete="SET NULL"`
    turns into an unbound-session state rather than a dangling reference.
    """
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [vanished] = recording_claims.created

    async with migrated_sessions.begin() as db:
        await db.execute(delete(ClaudeChatSession).where(ClaudeChatSession.session_id == vanished))
    assert await bound_session(conversations) is None, "the foreign key should have nulled the binding"

    await supervisor.supervise_once()

    assert len(recording_claims.created) == 2
    assert await bound_session(conversations) not in (None, vanished)


async def test_does_not_repeat_an_unchanged_status(
    supervisor, conversations, chat_store, recording_claims, announced
) -> None:
    """Every transition is reported, but a poll that changes nothing must not spam the room."""
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [session_id] = recording_claims.created
    # Provisioning already announced itself; the runner connecting is the next transition.
    assert await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id]) == "accepted"
    announced.clear()

    await supervisor.supervise_once()
    await supervisor.supervise_once()

    assert announced == [f"session {session_id} is ready"]


if __name__ == "__main__":
    pytest_bazel.main()
