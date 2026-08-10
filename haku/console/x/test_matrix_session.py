"""What the supervisor does when the room's session is missing, live, or dead."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

import pytest_bazel
from pydantic import SecretStr
from sqlalchemy import delete

from haku.console.config import ClaudeRuntimeConfig, MatrixConfig
from haku.console.database_schema import ClaudeChatSession
from haku.console.x.claude_chat import ClaudeChatService, ClaudeChatStore
from haku.console.x.matrix_session import MatrixConversationStore, MatrixSessionSupervisor

USER = "@haku:allegedly.works"
ROOM = "!room:allegedly.works"

CONFIG = MatrixConfig(
    homeserver="https://matrix.allegedly.works",
    user_id=USER,
    operator_user_id="@rai:allegedly.works",
    operator_subject="matrix-supervisor-operator",
)

RUNTIME = ClaudeRuntimeConfig.model_validate(
    {
        "namespace": "haku-claude-sandbox",
        "warm_pool": "haku-claude",
        "cwd": "/workspace",
        "session_ttl_seconds": 7200,
        "oauth_placeholder": "not-a-secret",
        "https_proxy": "http://proxy.test:8180",
        "ca_bundle": "/egress-proxy-ca/ca-certificates.crt",
        "no_proxy": "127.0.0.1,localhost",
        "mcp_url": "http://haku-console.test:9090/mcp",
        "mcp_static_agent_id": "00000000-0000-4000-8000-000000000001",
    }
)


class _RecordingClaims:
    """Kubernetes stands in; everything below it is the real store and the real service."""

    def __init__(self) -> None:
        self.created: list[UUID] = []
        self.deleted: list[UUID] = []
        self.tokens: dict[UUID, str] = {}

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: Any) -> None:
        del expires_at
        self.created.append(session_id)
        # The claim is where a test can reach the bridge credential; the service does not
        # return it, and it is what moves a session from provisioning to ready.
        self.tokens[session_id] = bridge_token

    async def delete(self, *, session_id: UUID) -> None:
        self.deleted.append(session_id)

    async def inspect(self, *, session_id: UUID) -> Any:
        raise AssertionError("the supervisor does not inspect provisioning")

    async def aclose(self) -> None:
        return None


@dataclass
class _Harness:
    supervisor: MatrixSessionSupervisor
    claims: _RecordingClaims
    conversations: MatrixConversationStore
    chat_store: ClaudeChatStore
    announced: list[str]

    async def bound_session(self) -> UUID | None:
        conversation = await self.conversations.load(USER)
        assert conversation is not None
        return conversation.session_id


async def _harness(sessions, engine, identities) -> _Harness:
    conversations = MatrixConversationStore(sessions)
    chat_store = ClaudeChatStore(sessions, engine)
    claims = _RecordingClaims()
    chat = ClaudeChatService(RUNTIME, chat_store, cast(Any, claims), mcp_token=SecretStr("unused"))
    announced: list[str] = []

    async def _announce(body: str) -> None:
        announced.append(body)

    supervisor = MatrixSessionSupervisor(
        CONFIG,
        conversations,
        chat,
        chat_store,
        identities,
        _announce,
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive `supervise_once`
    )
    return _Harness(supervisor, claims, conversations, chat_store, announced)


async def test_does_nothing_before_a_room_is_bound(migrated_sessions, migrated_engine, migrated_identity_store) -> None:
    """Nothing to serve, and nowhere to say so — provisioning here would be a sandbox nobody can reach."""
    harness = await _harness(migrated_sessions, migrated_engine, migrated_identity_store)

    await harness.supervisor.supervise_once()

    assert (harness.claims.created, harness.announced) == ([], [])


async def test_provisions_a_session_for_a_freshly_bound_room(
    migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    harness = await _harness(migrated_sessions, migrated_engine, migrated_identity_store)
    await harness.conversations.claim_room(USER, ROOM)

    await harness.supervisor.supervise_once()

    [session_id] = harness.claims.created
    assert await harness.bound_session() == session_id
    assert await harness.chat_store.status(session_id) == "provisioning"
    assert "provisioning a sandbox" in harness.announced[0]


async def test_leaves_a_live_session_alone(migrated_sessions, migrated_engine, migrated_identity_store) -> None:
    harness = await _harness(migrated_sessions, migrated_engine, migrated_identity_store)
    await harness.conversations.claim_room(USER, ROOM)
    await harness.supervisor.supervise_once()
    [live] = harness.claims.created

    await harness.supervisor.supervise_once()

    assert harness.claims.created == [live], "a live session was replaced"


async def test_replaces_a_failed_session(migrated_sessions, migrated_engine, migrated_identity_store) -> None:
    """A dead session over Matrix is invisible — the room would just stop answering."""
    harness = await _harness(migrated_sessions, migrated_engine, migrated_identity_store)
    await harness.conversations.claim_room(USER, ROOM)
    await harness.supervisor.supervise_once()
    [dead] = harness.claims.created
    await harness.chat_store.fail(dead, "the sandbox went away")

    await harness.supervisor.supervise_once()

    assert len(harness.claims.created) == 2
    assert await harness.bound_session() not in (None, dead)
    assert dead in harness.claims.deleted, "the dead session's claim must be swept before a new one is made"
    assert any("ended" in line for line in harness.announced)


async def test_replaces_a_session_whose_row_is_gone(
    migrated_sessions, migrated_engine, migrated_identity_store
) -> None:
    """A deleted session leaves the room bound but unserved, and the next pass re-provisions.

    The fake store this replaced pointed the binding at a session id that had never existed.
    Postgres refuses that: `matrix_conversation.session_id` is a foreign key, so a bound
    session always references a real row. What the schema does allow — and what this now
    covers — is the row being deleted underneath the binding, which `ondelete="SET NULL"`
    turns into an unbound-session state rather than a dangling reference.
    """
    harness = await _harness(migrated_sessions, migrated_engine, migrated_identity_store)
    await harness.conversations.claim_room(USER, ROOM)
    await harness.supervisor.supervise_once()
    [vanished] = harness.claims.created

    async with migrated_sessions.begin() as db:
        await db.execute(delete(ClaudeChatSession).where(ClaudeChatSession.session_id == vanished))
    assert await harness.bound_session() is None, "the foreign key should have nulled the binding"

    await harness.supervisor.supervise_once()

    assert len(harness.claims.created) == 2
    assert await harness.bound_session() not in (None, vanished)


async def test_does_not_repeat_an_unchanged_status(migrated_sessions, migrated_engine, migrated_identity_store) -> None:
    """Every transition is reported, but a poll that changes nothing must not spam the room."""
    harness = await _harness(migrated_sessions, migrated_engine, migrated_identity_store)
    await harness.conversations.claim_room(USER, ROOM)
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
