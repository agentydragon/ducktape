"""What the supervisor does when the room's session is missing, live, or dead."""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import delete

from haku.console.chat_models import ChatSessionStatus
from haku.console.database_schema import ClaudeChatSession
from haku.console.x.chat_notifications import ChatNotifications
from haku.console.x.claude_chat import BridgeAuthentication, ClaudeChatService, ClaudeChatStore, SpaSession
from haku.console.x.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM, MATRIX_USER, runtime_config
from haku.console.x.matrix_client import InboundMessage, MatrixError
from haku.console.x.matrix_session import MatrixConversationStore, MatrixSessionSupervisor, MatrixSurface
from haku.console.x.system_prompt import SystemPromptTemplate


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
    assert await chat_store.status(session_id) == ChatSessionStatus.PROVISIONING
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


async def test_replaces_a_session_whose_replica_stopped_renewing_its_lease(
    supervisor, conversations, chat_store, recording_claims, migrated_sessions, announced
) -> None:
    """The failure that took the room down on 2026-08-11.

    The session stayed `responding` because the replica running it went away without
    recording anything, and a live status was taken at face value here — so this method kept
    reporting "is responding" at a session that no longer existed anywhere but in a row.
    Supervision has to reclaim it, not believe it.
    """
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [orphan] = recording_claims.created
    async with migrated_sessions.begin() as db:
        chat = await db.get(ClaudeChatSession, orphan)
        assert chat is not None
        chat.status = ChatSessionStatus.RESPONDING
        chat.lease_expires_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=1)

    await supervisor.supervise_once()

    assert len(recording_claims.created) == 2, "the orphaned session was believed rather than replaced"
    assert await bound_session(conversations) not in (None, orphan)
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
    assert (
        await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
        == BridgeAuthentication.ACCEPTED
    )
    announced.clear()

    await supervisor.supervise_once()
    await supervisor.supervise_once()

    assert announced == [f"session {session_id} is ready"]


@pytest.fixture
async def bound(conversations: MatrixConversationStore, chat_store: ClaudeChatStore, operator_id: UUID) -> UUID:
    """A room bound to a real session row — `session_id` is a foreign key, not a free UUID."""
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    view, _ = await chat_store.create(operator_id, SpaSession())
    await conversations.set_session(MATRIX_USER, view.session_id)
    return view.session_id


# What `RoomChannel.recent_history` is, as a callable the fake room can be handed. Lives here
# rather than in the module now that the port declares the method itself.
RecentHistory = Callable[[int], Awaitable[Sequence[InboundMessage]]]


class _RecordingRoom:
    """A `RoomChannel` that keeps what it was told instead of speaking to a homeserver."""

    def __init__(self, history: RecentHistory) -> None:
        self._history = history
        self.shown: list[str] = []
        self.cleared = 0

    async def recent_history(self, limit: int) -> Sequence[InboundMessage]:
        return await self._history(limit)

    async def announce(self, body: str) -> None:
        raise AssertionError("the prompt path does not speak into the room")

    async def reply(self, body: str) -> None:
        raise AssertionError("the prompt path does not speak into the room")

    async def show_status(self, body: str) -> None:
        self.shown.append(body)

    async def clear_status(self) -> None:
        self.cleared += 1


def surface(history: RecentHistory) -> MatrixSurface:
    return MatrixSurface(
        MATRIX_CONFIG,
        runtime_config(),
        SystemPromptTemplate("{{ room_id }} {{ session_id }} {{ recent_messages | length }}"),
        _RecordingRoom(history),
    )


def served(*messages: InboundMessage) -> RecentHistory:
    async def _history(limit: int) -> tuple[InboundMessage, ...]:
        assert limit > 0
        return messages

    return _history


async def test_prompt_describes_the_room_it_is_given(bound: UUID) -> None:
    """No session filtering here any more: being called at all says this session serves it.

    The console selects this surface from the session's own `surface` column, so the room is
    an argument rather than something to look up and check.
    """
    assert await surface(served()).system_prompt(bound, MATRIX_ROOM) == f"{MATRIX_ROOM} {bound} 0"


async def test_prompt_survives_an_unreadable_room(bound: UUID) -> None:
    """A homeserver that will not serve history costs context, not the whole session."""

    async def unreadable(limit: int) -> tuple[InboundMessage, ...]:
        raise MatrixError("500: homeserver said no")

    assert await surface(unreadable).system_prompt(bound, MATRIX_ROOM) == f"{MATRIX_ROOM} {bound} 0"


async def test_prompt_carries_both_sides_of_the_room_history(bound: UUID) -> None:
    history = served(
        InboundMessage(room_id=MATRIX_ROOM, event_id="$a", sender=MATRIX_OPERATOR, body="hi", origin_server_ts=0),
        InboundMessage(room_id=MATRIX_ROOM, event_id="$b", sender=MATRIX_USER, body="hello", origin_server_ts=1),
    )

    assert await surface(history).system_prompt(bound, MATRIX_ROOM) == f"{MATRIX_ROOM} {bound} 2"


if __name__ == "__main__":
    pytest_bazel.main()
