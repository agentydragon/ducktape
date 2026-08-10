"""What the supervisor does when the room's session is missing, live, or dead."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest_bazel

from haku.console.config import MatrixConfig
from haku.console.database_schema import MatrixConversation
from haku.console.x.matrix_session import MatrixSessionSupervisor

USER = "@haku:allegedly.works"
ROOM = "!room:allegedly.works"
OPERATOR = UUID("00000000-0000-4000-8000-00000000beef")

CONFIG = MatrixConfig(
    homeserver="https://matrix.allegedly.works",
    user_id=USER,
    operator_user_id="@rai:allegedly.works",
    operator_subject="authentik-user-id",
)


@dataclass
class _FakeConversations:
    conversation: MatrixConversation | None = None

    async def load(self, user_id: str) -> MatrixConversation | None:
        return self.conversation

    async def set_session(self, user_id: str, session_id: UUID | None) -> None:
        assert self.conversation is not None
        self.conversation.session_id = session_id


@dataclass
class _FakeChat:
    """Stands in for ClaudeChatService: records provisioning, hands back a session id."""

    created: list[UUID] = field(default_factory=list)
    reconciled: int = 0

    async def create(self, operator_id: UUID):
        assert operator_id == OPERATOR
        session_id = uuid4()
        self.created.append(session_id)
        return _SessionView(session_id)

    async def reconcile_terminal_claims(self) -> None:
        self.reconciled += 1


@dataclass
class _SessionView:
    session_id: UUID


@dataclass
class _FakeChatStore:
    statuses: dict[UUID, str | None] = field(default_factory=dict)

    async def status(self, session_id: UUID) -> str | None:
        return self.statuses.get(session_id)


@dataclass
class _FakeIdentities:
    async def resolve_configured_external_user_key(self, key: str) -> UUID:
        assert key == "authentik-user-id"
        return OPERATOR


def _conversation(session_id: UUID | None) -> MatrixConversation:
    return MatrixConversation(
        user_id=USER, room_id=ROOM, session_id=session_id, joined_at=datetime.datetime.now(datetime.UTC)
    )


def _supervisor(conversation: MatrixConversation | None, statuses: dict[UUID, str | None] | None = None):
    conversations = _FakeConversations(conversation)
    chat = _FakeChat()
    store = _FakeChatStore(statuses or {})
    announced: list[str] = []

    async def _announce(body: str) -> None:
        announced.append(body)

    supervisor = MatrixSessionSupervisor(
        CONFIG,
        conversations,  # type: ignore[arg-type]
        chat,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        _FakeIdentities(),  # type: ignore[arg-type]
        _announce,
        engine=None,  # type: ignore[arg-type]
    )
    return supervisor, chat, conversations, announced


async def test_does_nothing_before_a_room_is_bound():
    """Nothing to serve, and nowhere to say so — provisioning here would be a sandbox nobody can reach."""
    supervisor, chat, _, announced = _supervisor(None)

    await supervisor.supervise_once()

    assert (chat.created, announced) == ([], [])


async def test_provisions_a_session_for_a_freshly_bound_room():
    supervisor, chat, conversations, announced = _supervisor(_conversation(None))

    await supervisor.supervise_once()

    [session_id] = chat.created
    assert conversations.conversation is not None
    assert conversations.conversation.session_id == session_id
    assert "provisioning a sandbox" in announced[0]


async def test_leaves_a_live_session_alone():
    session_id = uuid4()
    supervisor, chat, _, _ = _supervisor(_conversation(session_id), {session_id: "ready"})

    await supervisor.supervise_once()

    assert chat.created == []


async def test_replaces_a_failed_session():
    """A dead session over Matrix is invisible — the room would just stop answering."""
    dead = uuid4()
    supervisor, chat, conversations, announced = _supervisor(_conversation(dead), {dead: "failed"})

    await supervisor.supervise_once()

    assert len(chat.created) == 1
    assert conversations.conversation is not None
    assert conversations.conversation.session_id != dead
    assert chat.reconciled == 1, "the dead session's claim must be swept before a new one is made"
    assert any("ended" in line for line in announced)


async def test_replaces_a_session_whose_row_is_gone():
    """`status` returns None for a session that no longer exists; that is dead, not live."""
    vanished = uuid4()
    supervisor, chat, _, _ = _supervisor(_conversation(vanished), {})

    await supervisor.supervise_once()

    assert len(chat.created) == 1


async def test_does_not_repeat_an_unchanged_status():
    """Every transition is reported, but a poll that changes nothing must not spam the room."""
    session_id = uuid4()
    supervisor, _, _, announced = _supervisor(_conversation(session_id), {session_id: "ready"})

    await supervisor.supervise_once()
    await supervisor.supervise_once()

    assert announced == [f"session {session_id} is ready"]


if __name__ == "__main__":
    pytest_bazel.main()
