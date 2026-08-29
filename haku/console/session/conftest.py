"""Shared setup for the harness registration's tests — sessions, turns, frames, sandboxes.

Fixtures more than one module needs, stand-ins for what is genuinely outside the process (the
stand-ins themselves live in `../x/testing/`, so a non-pytest process can reach them too), and the
reads more than one module makes of the test database directly. Stores are never stood in for —
see <README.md> § Tests run against a real database.

**Nothing here may import a channel.** A second channel has to inherit this file unchanged, so
anything a room has — the homeserver identities, the room/session binding — belongs in
<../channels/matrix/conftest.py> instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import ChannelSurface
from haku.console.config import HarnessRegistrationConfig
from haku.console.conftest import console_sessions
from haku.console.conversation.item_vocabulary import ItemStatus, ItemType
from haku.console.database_schema import ChannelAttachmentRow, ConversationItem, Session
from haku.console.harnesses.kind import HarnessKind
from haku.console.identity.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.notifications.conversation_wakes import ConversationWakes
from haku.console.notifications.session_wakes import SessionWakes
from haku.console.session.conversation_views import SessionView
from haku.console.session.launch_identity import LaunchAuthorizer
from haku.console.session.runtime import SessionService
from haku.console.session.sandbox_allocation import SandboxAllocator
from haku.console.session.store import Store
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.console.x.runtime import HarnessRegistry
from haku.console.x.runtime_catalog import execution_registry, harness_registration
from haku.console.x.testing.recording_claims import RecordingClaims

OPERATOR_SUBJECT = "authentik-user-id"
TEST_AGENT_ID = UUID("00000000-0000-4000-8000-000000000001")
TEST_ACCESS_PROFILE_ID = "no_auto_approval"


def runtime_config(**overrides: object) -> HarnessRegistrationConfig:
    values: dict[str, object] = {
        "agent_id": str(TEST_AGENT_ID),
        "namespace": "haku-claude-sandbox",
        "warm_pool": "haku-claude",
        "claim_prefix": "claude",
        "harness_label": "claude-chat",
        "cwd": "/workspace",
        "session_ttl_seconds": 7200,
        "https_proxy": "http://proxy.test:8180",
        "ca_bundle": "/egress-proxy-ca/ca-certificates.crt",
        "no_proxy": "127.0.0.1,localhost,.svc,.svc.cluster.local,kubernetes.default.svc,10.0.0.0/8",
        "mcp_url": "http://haku-console.test:9090/mcp",
        "implementation": {
            "kind": "claude_code",
            "api_base_url": "http://litellm.test:4000",
            "model": "anthropic-max20/ant-messages/claude-sonnet-5",
            "haiku_model": "anthropic-max20/ant-messages/claude-haiku-4-5-20251001",
            "auth_token_placeholder": "not-a-secret",
        },
    }
    values.update(overrides)
    return HarnessRegistrationConfig(**values)


def configured_harnesses(
    claims: RecordingClaims,
    *,
    config: HarnessRegistrationConfig | None = None,
    system_prompt: SystemPromptTemplate | None = None,
) -> HarnessRegistry:
    return execution_registry(
        harness_registration(
            config or runtime_config(),
            claims,
            system_prompt=system_prompt or SystemPromptTemplate(""),
            access_profile_id=TEST_ACCESS_PROFILE_ID,
        )
    )


@pytest.fixture
def recording_claims() -> RecordingClaims:
    return RecordingClaims()


@pytest.fixture
async def test_agent(
    migrated_db_url: str, migrated_identity_store: PostgresOperatorIdentityStore, operator_id: UUID
) -> None:
    """Seed the explicit Agent identity used by the channel-neutral session fixtures."""
    authority = PostgresAgentAuthority(
        console_sessions(migrated_db_url),
        public_base_url="https://haku.test",
        operator_identity_store=migrated_identity_store,
        access_profiles=(TEST_ACCESS_PROFILE_ID,),
        default_access_profile_id=TEST_ACCESS_PROFILE_ID,
    )
    await authority.reconcile_static_agents(
        [
            StaticAgentDefinition(
                agent_id=TEST_AGENT_ID,
                display_name="Session Test Agent",
                operator_id=operator_id,
                secret_reference="env:SESSION_TEST_AGENT",
                token_fingerprint=fingerprint_static_token("session-test-agent-token"),
                access_profile_id=TEST_ACCESS_PROFILE_ID,
            )
        ]
    )


class _ProvisioningTestStore(Store):
    """Keep older focused tests concise while the production writer now starts idle."""

    async def create(
        self,
        operator_id: UUID,
        *,
        conversation_id: UUID | None = None,
        agent_id: UUID | None = None,
        access_profile_id: str | None = None,
        harness_kind: HarnessKind | None = None,
        launch_authorizer: LaunchAuthorizer | None = None,
    ) -> tuple[SessionView, str]:
        if launch_authorizer is not None:
            return await super().create(
                operator_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
                access_profile_id=access_profile_id,
                harness_kind=harness_kind,
                launch_authorizer=launch_authorizer,
            )
        return await self._create_provisioning_for_test(
            operator_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            access_profile_id=access_profile_id,
            harness_kind=harness_kind,
        )


@pytest.fixture
async def session_store(
    migrated_sessions: async_sessionmaker[AsyncSession], test_agent: None
) -> _ProvisioningTestStore:
    return _ProvisioningTestStore(migrated_sessions)


@pytest.fixture
async def session_wakes(migrated_db_url: str) -> AsyncIterator[SessionWakes]:
    """A real listener against the test database — the plumbing is the thing under test."""
    wakes = SessionWakes(migrated_db_url)
    await wakes.start()
    try:
        yield wakes
    finally:
        await wakes.aclose()


@pytest.fixture
async def conversation_wakes(migrated_db_url: str) -> AsyncIterator[ConversationWakes]:
    """A real listener against the test database — the plumbing is the thing under test."""
    wakes = ConversationWakes(migrated_db_url)
    await wakes.start()
    try:
        yield wakes
    finally:
        await wakes.aclose()


@pytest.fixture
def chat_service(
    session_store: Store, recording_claims: RecordingClaims, session_wakes: SessionWakes
) -> SessionService:
    return SessionService(configured_harnesses(recording_claims), session_store, session_wakes)


@pytest.fixture
def allocator(
    chat_service: SessionService, session_store: Store, session_wakes: SessionWakes, migrated_engine: AsyncEngine
) -> SandboxAllocator:
    """The channel-neutral demand reconciler over the same database the test writes."""
    return SandboxAllocator(chat_service, session_store, session_wakes, migrated_engine)


@pytest.fixture
async def operator_id(migrated_identity_store: PostgresOperatorIdentityStore) -> UUID:
    """The canonical Operator these tests act as. One key for every test; the database is per-test."""
    return await migrated_identity_store.resolve_configured_external_user_key(OPERATOR_SUBJECT)


async def make_idle(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> None:
    """Return a `_ProvisioningTestStore` session to the unallocated state production creates in.

    Clearing the credential and its lease is the whole transition: idle is what those facts derive.
    """
    async with sessions.begin() as db:
        await db.execute(
            update(Session)
            .where(Session.session_id == session_id)
            .values(bridge_token_fingerprint=None, session_token_fingerprint=None, lease_expires_at=None)
        )


async def attach_channel(sessions: async_sessionmaker[AsyncSession], session_id: UUID, address: str) -> None:
    """Give the conversation this session runs a channel holding a copy of it, at *address*.

    What a room's ingress leaves behind, and what makes a reply this session produces owed
    somewhere: the outbox row is addressed off the attachment, not off the session.
    """
    async with sessions.begin() as db:
        db.add(
            ChannelAttachmentRow(
                attachment_id=uuid4(),
                conversation_id=await db.scalar(
                    select(Session.conversation_id).where(Session.session_id == session_id)
                ),
                surface=ChannelSurface.MATRIX,
                address=address,
                attached_at=datetime.now(UTC),
            )
        )


async def session_items(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> list[ConversationItem]:
    """This session's stored `conversation_item` rows, in opening order, for row-level assertions.

    The rows and not a read model: what these tests pin is what the fold materialised — an item
    still open, a call resumed onto the row its predecessor minted — which the read surfaces
    deliberately do not carry.
    """
    async with sessions() as db:
        return list(
            (
                await db.scalars(
                    select(ConversationItem)
                    .where(ConversationItem.session_id == session_id)
                    .order_by(ConversationItem.opened_seq)
                )
            ).all()
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
