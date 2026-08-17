"""Shared setup for the runtime's tests — sessions, turns, frames, sandboxes.

Fixtures more than one module needs, stand-ins for what is genuinely outside the process (the
stand-ins themselves live in `testing/`, so a non-pytest process can reach them too), and the reads
more than one module makes of the test database directly. Stores are never stood in for — see
<README.md> § Tests run against a real database.

**Nothing here may import a channel.** A second channel has to inherit this file unchanged, so
anything a room has — the homeserver identities, the room/session binding — belongs in
<channels/matrix/conftest.py> instead (<README.md> § The runtime's conftest names no channel).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import ChatSurface
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ChatAttachment, Session, SessionOutbox
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import SessionStore
from haku.console.x.testing.recording_claims import RecordingClaims

OPERATOR_SUBJECT = "authentik-user-id"


def runtime_config(**overrides: object) -> ClaudeRuntimeConfig:
    values: dict[str, object] = {
        "namespace": "haku-claude-sandbox",
        "warm_pool": "haku-claude",
        "cwd": "/workspace",
        "session_ttl_seconds": 7200,
        "oauth_placeholder": "not-a-secret",
        "https_proxy": "http://proxy.test:8180",
        "ca_bundle": "/egress-proxy-ca/ca-certificates.crt",
        "no_proxy": "127.0.0.1,localhost,.svc,.svc.cluster.local,kubernetes.default.svc,10.0.0.0/8",
        "mcp_url": "http://haku-console.test:9090/mcp",
        "mcp_static_agent_id": "00000000-0000-4000-8000-000000000001",
        "system_prompt_template": "cluster/k8s/haku/console/matrix_system_prompt.md.j2",
    }
    values.update(overrides)
    return ClaudeRuntimeConfig.model_validate(values)


MCP_TOKEN = SecretStr("haku-static-bearer")


@pytest.fixture
def recording_claims() -> RecordingClaims:
    return RecordingClaims()


@pytest.fixture
def chat_store(migrated_sessions: async_sessionmaker[AsyncSession]) -> SessionStore:
    return SessionStore(migrated_sessions)


@pytest.fixture
async def notifications(migrated_db_url: str) -> AsyncIterator[SessionNotifications]:
    """A real listener against the test database — the plumbing is the thing under test."""
    channel = SessionNotifications(migrated_db_url)
    await channel.start()
    try:
        yield channel
    finally:
        await channel.aclose()


@pytest.fixture
def chat_service(
    chat_store: SessionStore, recording_claims: RecordingClaims, notifications: SessionNotifications
) -> SessionService:
    return SessionService(runtime_config(), chat_store, recording_claims, notifications, mcp_token=MCP_TOKEN)


@pytest.fixture
async def operator_id(migrated_identity_store: PostgresOperatorIdentityStore) -> UUID:
    """The canonical Operator these tests act as. One key for every test; the database is per-test."""
    return await migrated_identity_store.resolve_configured_external_user_key(OPERATOR_SUBJECT)


async def attach_channel(sessions: async_sessionmaker[AsyncSession], session_id: UUID, address: str) -> None:
    """Give the conversation this session runs a channel holding a copy of it, at *address*.

    What a room's ingress leaves behind, and what makes a reply this session produces owed
    somewhere: the outbox row is addressed off the attachment, not off the session.
    """
    async with sessions.begin() as db:
        db.add(
            ChatAttachment(
                attachment_id=uuid4(),
                conversation_id=await db.scalar(
                    select(Session.conversation_id).where(Session.session_id == session_id)
                ),
                surface=ChatSurface.MATRIX,
                address=address,
                attached_at=datetime.now(UTC),
            )
        )


async def queued_for_the_room(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> list[str]:
    """What this session put in the room's outbox, oldest first.

    A turn hands its answer to nothing: it writes a row with the message, and
    `channels/matrix/outbox.py` says it. So what the room is owed is asked of the rows.
    """
    async with sessions() as db:
        return list(
            await db.scalars(
                select(SessionOutbox.body)
                .where(SessionOutbox.session_id == session_id)
                .order_by(SessionOutbox.created_at, SessionOutbox.outbox_id)
            )
        )


async def age_lease(sessions: async_sessionmaker[AsyncSession], session_id: UUID, *, seconds_ago: int) -> None:
    async with sessions.begin() as db:
        chat = await db.get(Session, session_id)
        assert chat is not None
        chat.lease_expires_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)


async def lease_of(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> tuple[str | None, datetime]:
    async with sessions() as db:
        chat = await db.get(Session, session_id)
        assert chat is not None
        return chat.lease_holder, chat.lease_expires_at
