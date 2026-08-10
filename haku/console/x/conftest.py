"""Shared setup for the experimental console surfaces' tests.

Two kinds of thing live here: fixtures more than one module needs, and stand-ins for what is
genuinely outside the process. Stores are never stood in for — see <README.md> § Tests run
against a real database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.config import ClaudeRuntimeConfig, MatrixConfig
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.claude_chat import ClaudeChatService, ClaudeChatStore, _provisioning_view

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
    }
    values.update(overrides)
    return ClaudeRuntimeConfig.model_validate(values)


MCP_TOKEN = SecretStr("haku-static-bearer")


class RecordingClaims:
    """The `SandboxClaims` surface, recording instead of reaching Kubernetes."""

    def __init__(self) -> None:
        self.created: list[UUID] = []
        self.deleted: list[UUID] = []
        self.tokens: dict[UUID, str] = {}

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        assert expires_at > datetime.now(expires_at.tzinfo)
        self.created.append(session_id)
        # The claim is where a test reaches the bridge credential: the store mints it and
        # `ClaudeChatService.create` does not hand it back.
        self.tokens[session_id] = bridge_token

    async def delete(self, *, session_id: UUID) -> None:
        self.deleted.append(session_id)

    async def inspect(self, *, session_id: UUID) -> Any:
        return _provisioning_view(f"claude-{session_id.hex}", step="claim_created")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def recording_claims() -> RecordingClaims:
    return RecordingClaims()


@pytest.fixture
def chat_store(migrated_sessions: async_sessionmaker[AsyncSession], migrated_engine: AsyncEngine) -> ClaudeChatStore:
    return ClaudeChatStore(migrated_sessions, migrated_engine)


@pytest.fixture
def chat_service(chat_store: ClaudeChatStore, recording_claims: RecordingClaims) -> ClaudeChatService:
    return ClaudeChatService(runtime_config(), chat_store, recording_claims, mcp_token=MCP_TOKEN)


@pytest.fixture
async def operator_id(migrated_identity_store: PostgresOperatorIdentityStore) -> UUID:
    """The canonical Operator these tests act as.

    One key for every test rather than a per-test string: the database is per-test, so the
    keys were only ever distinct out of caution.
    """
    return await migrated_identity_store.resolve_configured_external_user_key(OPERATOR_SUBJECT)
