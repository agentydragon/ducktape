"""What the supervisor does when the room's session is missing, live, or dead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import delete

from haku.console.database_schema import ClaudeChatSession
from haku.console.x.claude_chat import ClaudeChatStore
from haku.console.x.conftest import MATRIX_CONFIG, MATRIX_ROOM, MATRIX_USER, RecordingClaims
from haku.console.x.matrix_session import MatrixConversationStore, MatrixSessionSupervisor


@dataclass
class _Harness:
    supervisor: MatrixSessionSupervisor
    claims: RecordingClaims
    conversations: MatrixConversationStore
    chat_store: ClaudeChatStore
    announced: list[str]

    async def bound_session(self) -> UUID | None:
        conversation = await self.conversations.load(MATRIX_USER)
        assert conversation is not None
        return conversation.session_id


@pytest.fixture
def harness(migrated_sessions, chat_store, chat_service, recording_claims, migrated_identity_store) -> _Harness:
    """The supervisor over real stores, with only Kubernetes and the announce sink stood in."""
    sessions, claims, identities = migrated_sessions, recording_claims, migrated_identity_store
    conversations = MatrixConversationStore(sessions)
    announced: list[str] = []

    async def _announce(body: str) -> None:
        announced.append(body)

    supervisor = MatrixSessionSupervisor(
        MATRIX_CONFIG,
        conversations,
        chat_service,
        chat_store,
        identities,
        _announce,
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive `supervise_once`
    )
    return _Harness(supervisor, claims, conversations, chat_store, announced)


async def test_does_nothing_before_a_room_is_bound(harness: _Harness) -> None:
    """Nothing to serve, and nowhere to say so — provisioning here would be a sandbox nobody can reach."""

    await harness.supervisor.supervise_once()

    assert (harness.claims.created, harness.announced) == ([], [])


async def test_provisions_a_session_for_a_freshly_bound_room(harness: _Harness) -> None:
    await harness.conversations.claim_room(MATRIX_USER, MATRIX_ROOM)

    await harness.supervisor.supervise_once()

    [session_id] = harness.claims.created
    assert await harness.bound_session() == session_id
    assert await harness.chat_store.status(session_id) == "provisioning"
    assert "provisioning a sandbox" in harness.announced[0]


async def test_leaves_a_live_session_alone(harness: _Harness) -> None:
    await harness.conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await harness.supervisor.supervise_once()
    [live] = harness.claims.created

    await harness.supervisor.supervise_once()

    assert harness.claims.created == [live], "a live session was replaced"


async def test_replaces_a_failed_session(harness: _Harness) -> None:
    """A dead session over Matrix is invisible — the room would just stop answering."""
    await harness.conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await harness.supervisor.supervise_once()
    [dead] = harness.claims.created
    await harness.chat_store.fail(dead, "the sandbox went away")

    await harness.supervisor.supervise_once()

    assert len(harness.claims.created) == 2
    assert await harness.bound_session() not in (None, dead)
    assert dead in harness.claims.deleted, "the dead session's claim must be swept before a new one is made"
    assert any("ended" in line for line in harness.announced)


async def test_replaces_a_session_whose_row_is_gone(harness: _Harness, migrated_sessions) -> None:
    """A deleted session leaves the room bound but unserved, and the next pass re-provisions.

    The fake store this replaced pointed the binding at a session id that had never existed.
    Postgres refuses that: `matrix_conversation.session_id` is a foreign key, so a bound
    session always references a real row. What the schema does allow — and what this now
    covers — is the row being deleted underneath the binding, which `ondelete="SET NULL"`
    turns into an unbound-session state rather than a dangling reference.
    """
    await harness.conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await harness.supervisor.supervise_once()
    [vanished] = harness.claims.created

    async with migrated_sessions.begin() as db:
        await db.execute(delete(ClaudeChatSession).where(ClaudeChatSession.session_id == vanished))
    assert await harness.bound_session() is None, "the foreign key should have nulled the binding"

    await harness.supervisor.supervise_once()

    assert len(harness.claims.created) == 2
    assert await harness.bound_session() not in (None, vanished)


async def test_does_not_repeat_an_unchanged_status(harness: _Harness) -> None:
    """Every transition is reported, but a poll that changes nothing must not spam the room."""
    await harness.conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await harness.supervisor.supervise_once()
    [session_id] = harness.claims.created
    # Provisioning already announced itself; the runner connecting is the next transition.
    assert await harness.chat_store.authenticate_bridge(session_id, harness.claims.tokens[session_id]) == "accepted"
    harness.announced.clear()

    await harness.supervisor.supervise_once()
    await harness.supervisor.supervise_once()

    assert harness.announced == [f"session {session_id} is ready"]


if __name__ == "__main__":
    pytest_bazel.main()
