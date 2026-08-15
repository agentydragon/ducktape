"""Shared setup for the experimental console surfaces' tests.

Two kinds of thing live here: fixtures more than one module needs, and fixtures handing out
stand-ins for what is genuinely outside the process (the stand-ins themselves live in `testing/`,
so a non-pytest process can reach them too). Stores are never stood in for — see <README.md>
§ Tests run against a real database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import ClaudeRuntimeConfig, MatrixConfig
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.claude_chat import SessionService, SessionStore
from haku.console.x.matrix_session import MatrixConversationStore
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.testing.recording_claims import RecordingClaims

MATRIX_USER = "@haku:allegedly.works"
MATRIX_OPERATOR = "@rai:allegedly.works"
MATRIX_ROOM = "!room:allegedly.works"
OPERATOR_SUBJECT = "authentik-user-id"

MATRIX_CONFIG = MatrixConfig(
    homeserver="https://matrix.allegedly.works",
    user_id=MATRIX_USER,
    operator_user_id=MATRIX_OPERATOR,
    operator_subject=OPERATOR_SUBJECT,
)


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
def conversations(migrated_sessions: async_sessionmaker[AsyncSession]) -> MatrixConversationStore:
    return MatrixConversationStore(migrated_sessions)


@pytest.fixture
async def operator_id(migrated_identity_store: PostgresOperatorIdentityStore) -> UUID:
    """The canonical Operator these tests act as.

    One key for every test rather than a per-test string: the database is per-test, so the
    keys were only ever distinct out of caution.
    """
    return await migrated_identity_store.resolve_configured_external_user_key(OPERATOR_SUBJECT)
