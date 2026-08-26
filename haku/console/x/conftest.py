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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import ChatSurface, ItemStatus, ItemType, RuntimeKind
from haku.console.config import ClaudeRuntimeConfig
from haku.console.database_schema import ChatAttachment, ConversationItem, Session
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.claude_code.client import cli_over_websocket
from haku.console.x.launch_identity import LaunchAuthorizer
from haku.console.x.runtime import RuntimeClientFactory, RuntimeRegistry
from haku.console.x.runtime_catalog import claude_registration, execution_registry, projection_registry
from haku.console.x.sandbox_allocation import SandboxAllocator
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import SessionStore
from haku.console.x.session_views import SessionView
from haku.console.x.system_prompt import SystemPromptTemplate
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
        "system_prompt_template": "cluster/k8s/haku/console/chat_system_prompt.md.j2",
    }
    values.update(overrides)
    return ClaudeRuntimeConfig(**values)


def configured_runtimes(
    claims: RecordingClaims,
    *,
    config: ClaudeRuntimeConfig | None = None,
    system_prompt: SystemPromptTemplate | None = None,
    client_factory: RuntimeClientFactory = cli_over_websocket,
) -> RuntimeRegistry:
    return execution_registry(
        claude_registration(
            config or runtime_config(),
            claims,
            system_prompt=system_prompt or SystemPromptTemplate(""),
            client_factory=client_factory,
        )
    )


@pytest.fixture
def recording_claims() -> RecordingClaims:
    return RecordingClaims()


class _ProvisioningTestStore(SessionStore):
    """Keep older focused tests concise while the production writer now starts idle."""

    async def create(
        self,
        operator_id: UUID,
        *,
        conversation_id: UUID | None = None,
        agent_id: UUID | None = None,
        access_profile_id: str | None = None,
        runtime_kind: RuntimeKind | None = None,
        launch_authorizer: LaunchAuthorizer | None = None,
    ) -> tuple[SessionView, str]:
        if launch_authorizer is not None:
            return await super().create(
                operator_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                access_profile_id=access_profile_id,
                runtime_kind=runtime_kind,
                launch_authorizer=launch_authorizer,
            )
        return await self._create_provisioning_for_test(
            operator_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            access_profile_id=access_profile_id,
            runtime_kind=runtime_kind,
        )


@pytest.fixture
def chat_store(migrated_sessions: async_sessionmaker[AsyncSession]) -> _ProvisioningTestStore:
    return _ProvisioningTestStore(migrated_sessions, projection_registry())


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
    return SessionService(configured_runtimes(recording_claims), chat_store, notifications)


@pytest.fixture
def allocator(
    chat_service: SessionService,
    chat_store: SessionStore,
    notifications: SessionNotifications,
    migrated_engine: AsyncEngine,
) -> SandboxAllocator:
    """The channel-neutral demand reconciler over the same database the test writes."""
    return SandboxAllocator(chat_service, chat_store, notifications, migrated_engine)


@pytest.fixture
async def operator_id(migrated_identity_store: PostgresOperatorIdentityStore) -> UUID:
    """The canonical Operator these tests act as. One key for every test; the database is per-test."""
    return await migrated_identity_store.resolve_configured_external_user_key(OPERATOR_SUBJECT)


async def make_idle(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> None:
    """Put a test session in the writer state the next rollout will create.

    Production deliberately has no idle writer in this compatibility release. Tests use a direct
    row transition so the readers and allocation paths can be proven before that writer lands.
    """
    async with sessions.begin() as db:
        await db.execute(
            update(Session)
            .where(Session.session_id == session_id)
            .values(status="idle", bridge_token_fingerprint=None, lease_expires_at=None)
        )


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


async def answers(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> list[str]:
    """What this session said, oldest first — its completed message items and nothing else.

    A turn hands its answer to nothing and addresses no channel: it writes the log, the items follow
    from it, and each attached channel reads forward from its own cursor and decides what it owes.
    So what was said is asked of the items, and what a *room* was owed is a question for that
    channel's own tests.
    """
    async with sessions() as db:
        return list(
            await db.scalars(
                select(ConversationItem.item_text)
                .where(
                    ConversationItem.session_id == session_id,
                    ConversationItem.item_type == ItemType.MESSAGE,
                    ConversationItem.status == ItemStatus.COMPLETE,
                )
                .order_by(ConversationItem.opened_seq)
            )
        )


async def age_lease(sessions: async_sessionmaker[AsyncSession], session_id: UUID, *, seconds_ago: int) -> None:
    async with sessions.begin() as db:
        chat = await db.get(Session, session_id)
        assert chat is not None
        chat.lease_expires_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)


async def lease_of(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> tuple[str | None, datetime | None]:
    async with sessions() as db:
        chat = await db.get(Session, session_id)
        assert chat is not None
        return chat.lease_holder, chat.lease_expires_at
