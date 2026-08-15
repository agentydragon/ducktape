"""What the supervisor does when the room's session is missing, live, or dead."""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import delete, select

from haku.console.chat_models import ChatSessionStatus
from haku.console.database_schema import ClaudeChatSession
from haku.console.x.chat_notifications import ChatNotifications
from haku.console.x.claude_chat import (
    ADOPTION_GRACE,
    BridgeAuthentication,
    ClaudeChatService,
    ClaudeChatStore,
    SpaSession,
)
from haku.console.x.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM, MATRIX_USER, runtime_config
from haku.console.x.matrix_client import EventTag, InboundMessage, MatrixError, RoomEventKind
from haku.console.x.matrix_session import NOTHING_SAID, MatrixConversationStore, MatrixSessionSupervisor, MatrixSurface
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
    # The status alone says a session died; only the reason says which failure it was, and the
    # room is the one place an operator is looking.
    assert any("the sandbox went away" in line for line in announced)


async def test_the_pointer_moves_while_each_session_keeps_the_room_it_served(
    supervisor, conversations, chat_store, recording_claims, migrated_sessions
) -> None:
    """The binding lives in two places and they answer different questions.

    `matrix_conversation.session_id` is the pointer — which session the room talks to *now* — and
    `claude_chat_sessions.room_id` is the history, written once and never moved. No SQL constraint
    can state the agreement between them (a CHECK sees one row; a composite foreign key would need
    `room_id` in this table's key), so the supervisor is its only maintainer and this is where that
    is checked. Without the history half, a replaced Matrix session became indistinguishable from
    an SPA one the moment the supervisor moved on.
    """
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [first] = recording_claims.created
    await chat_store.fail(first, "the sandbox went away")

    await supervisor.supervise_once()

    second = await bound_session(conversations)
    assert second not in (None, first), "the pointer follows the live session"
    async with migrated_sessions() as db:
        rooms = {
            row.session_id: row.room_id
            for row in await db.scalars(select(ClaudeChatSession).order_by(ClaudeChatSession.created_at))
        }
    assert rooms == {first: MATRIX_ROOM, second: MATRIX_ROOM}, "each session still says which room it served"


async def test_replaces_a_session_whose_replica_stopped_renewing_its_lease(
    supervisor, conversations, chat_store, recording_claims, migrated_sessions, announced
) -> None:
    """The failure that took the room down on 2026-08-11.

    The session stayed `responding` because the replica running it went away without
    recording anything, and a live status was taken at face value here — so this method kept
    reporting "is responding" at a session that no longer existed anywhere but in a row.
    Supervision has to reclaim it, not believe it — but only once the lease has been adoptable
    for a whole `ADOPTION_GRACE` and no runner took it, which is what makes a console roll
    survivable rather than fatal.
    """
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    await supervisor.supervise_once()
    [orphan] = recording_claims.created
    async with migrated_sessions.begin() as db:
        chat = await db.get(ClaudeChatSession, orphan)
        assert chat is not None
        chat.status = ChatSessionStatus.RESPONDING
        chat.lease_expires_at = datetime.datetime.now(datetime.UTC) - ADOPTION_GRACE - datetime.timedelta(seconds=1)

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
        self.typing: list[bool] = []
        self.announced: list[tuple[str, RoomEventKind]] = []
        self.replied: list[tuple[str, EventTag]] = []

    async def recent_history(self, limit: int) -> Sequence[InboundMessage]:
        return await self._history(limit)

    async def announce(self, body: str, kind: RoomEventKind = RoomEventKind.LIFECYCLE) -> None:
        self.announced.append((body, kind))

    async def reply(self, body: str, tag: EventTag) -> None:
        self.replied.append((body, tag))

    async def show_status(self, body: str, session_id: UUID | None = None) -> None:
        self.shown.append(body)

    async def clear_status(self) -> None:
        self.cleared += 1

    async def set_typing(self, active: bool) -> None:
        self.typing.append(active)


def surface_and_room(history: RecentHistory) -> tuple[MatrixSurface, _RecordingRoom]:
    room = _RecordingRoom(history)
    template = SystemPromptTemplate("{{ room_id }} {{ session_id }} {{ recent_messages | length }}")
    return MatrixSurface(MATRIX_CONFIG, runtime_config(), template, room), room


def surface(history: RecentHistory) -> MatrixSurface:
    return surface_and_room(history)[0]


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


async def test_building_a_prompt_says_nothing_into_the_room(bound: UUID) -> None:
    """Reading the room's history is not a reason to post in it."""
    built, room = surface_and_room(served())

    await built.system_prompt(bound, MATRIX_ROOM)

    assert (room.announced, room.replied) == ([], [])


async def test_an_answer_reaches_the_room_tagged_with_its_row(bound: UUID) -> None:
    message_id = uuid4()
    delivering, room = surface_and_room(served())

    await delivering.deliver(MATRIX_ROOM, "  because the disk was full  ", bound, message_id, "msg_01abc")

    [(body, tag)] = room.replied
    assert body == "because the disk was full", "trimmed, since the room shows what it is given"
    assert (tag.kind, tag.session_id, tag.message_id, tag.agent_message_id) == (
        RoomEventKind.REPLY,
        bound,
        message_id,
        "msg_01abc",
    )


async def test_a_turn_with_nothing_to_say_says_so(bound: UUID) -> None:
    """R11.2 has no silence token, and the empty string was quietly working as one: a turn that
    produced no text left the room with nothing at all, which from the operator's side is
    indistinguishable from an answer the console lost.

    A notice rather than a reply, because nothing was said — this is the console reporting an
    outcome, not the agent talking.
    """
    delivering, room = surface_and_room(served())

    await delivering.deliver(MATRIX_ROOM, "   ", bound)

    assert room.replied == []
    assert room.announced == [(NOTHING_SAID, RoomEventKind.NARRATION)]


if __name__ == "__main__":
    pytest_bazel.main()
