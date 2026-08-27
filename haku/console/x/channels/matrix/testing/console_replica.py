"""One console replica, as its own process, for the Matrix full-stack end-to-end test.

<../test_fullstack_e2e.py> is about what the room ends up containing across a console going away,
so the console has to be something that can *go away* — a process the test starts, stops and starts
again, on one port, against one database. Everything it composes is the production wiring from
`haku.console.app`. What is replaced is what would otherwise be Kubernetes, plus one deliberate
fault:

- **`FileSandboxClaims`** writes the claim a `SandboxClaim` controller would have acted on. The
  sandbox must outlive this process — that is the whole subject of the adoption case — so the
  runner is started by the *test*, which watches that directory, rather than as a child here.
- **`HAKU_E2E_REFUSE_NEXT_REPLY`** arms a single refused send. A homeserver that refuses one
  (a 429 past `MAX_RATE_LIMIT_RETRIES`, a transient 5xx, a room state that briefly forbids it) is
  not reproducible on demand against a healthy Synapse, and what is under test is what the console
  does with a failed send rather than how it came to fail.

`HOSTNAME` is what `session_runtime.REPLICA` reads, so each replica must be given a distinct one:
it is what the session lease records as its holder, and two replicas sharing it would make an
adoption look like the same process reconnecting to itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.config import MatrixConfig, RuntimeRegistrationConfig
from haku.console.operator_identity import OperatorIdentityTrust
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.channels.matrix.client import MatrixError
from haku.console.x.channels.matrix.conversation import MatrixConversationStore, MatrixTurns
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.channels.matrix.outbox import PendingReply, RoomOutbox
from haku.console.x.channels.matrix.outbox_wake import OutboxWakes
from haku.console.x.channels.matrix.revisions import RevisionLog
from haku.console.x.channels.matrix.room_copy import RoomCopy
from haku.console.x.channels.matrix.sync import MatrixSyncService, MatrixSyncStore
from haku.console.x.conversation_history import ConversationHistory
from haku.console.x.conversation_runtime import ConversationRuntime
from haku.console.x.runtime_catalog import execution_registry, runtime_registration
from haku.console.x.sandbox_allocation import SandboxAllocator
from haku.console.x.sandbox_claims import SandboxProvisioningView
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_runtime import SessionService, internal_router
from haku.console.x.session_store import SessionStore
from haku.console.x.subscription import ConversationStream
from haku.console.x.system_prompt import SystemPromptTemplate
from haku.console.x.testing.recording_claims import fixed_provisioning_view

logger = logging.getLogger("haku.console.x.channels.matrix.testing.console_replica")

# The one identity namespace this replica resolves the configured operator subject in. Any value
# works as long as every replica in a run agrees, since it is only ever resolved to a local UUID.
TRUST_DOMAIN = "auth.test/authentik-user-id/v1"
TRUSTED_ISSUER = "https://auth.test/application/o/haku-console/"


class FileSandboxClaims:
    """The `SandboxClaims` surface, writing what Kubernetes would have been asked to run.

    One file per live claim, named by session, holding the session credential the sandbox needs.
    Written by rename so a watcher never reads a half-written claim.
    """

    def __init__(self, directory: Path):
        self._directory = directory

    def _path(self, session_id: UUID) -> Path:
        return self._directory / f"{session_id}.json"

    async def create(self, *, session_id: UUID, bridge_token: str, expires_at: datetime) -> None:
        del expires_at
        staged = self._directory / f".{session_id}.staged"
        staged.write_text(json.dumps({"session_id": str(session_id), "bridge_token": bridge_token}))
        staged.replace(self._path(session_id))
        logger.info("claim created for session %s", session_id)

    async def renew(self, *, session_id: UUID, expires_at: datetime) -> None:
        del session_id, expires_at

    async def delete(self, *, session_id: UUID) -> None:
        self._path(session_id).unlink(missing_ok=True)
        logger.info("claim deleted for session %s", session_id)

    async def inspect(self, *, session_id: UUID) -> SandboxProvisioningView:
        return fixed_provisioning_view(session_id)

    def observation_error(self, *, session_id: UUID, error: str) -> SandboxProvisioningView:
        return fixed_provisioning_view(session_id).model_copy(update={"observation_error": error})

    async def aclose(self) -> None:
        return None


class RefusingSyncService(MatrixSyncService):
    """The sync service, with one refused send the test arms by creating a file.

    The refusal lands on `post_reply` because that is where a homeserver refusal lands. The file is
    consumed rather than only read, so the test can both arm the fault and observe that it fired.
    """

    def __init__(self, *args: Any, armed: Path, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._armed = armed

    async def post_reply(self, reply: PendingReply) -> str:
        if self._armed.exists():
            self._armed.unlink()
            logger.warning("refusing to post %r, as armed", reply.body)
            raise MatrixError("429: simulated homeserver refusal")
        return await super().post_reply(reply)


def _environment(name: str) -> str:
    if (value := os.environ.get(name)) is None:
        raise RuntimeError(f"{name} is required")
    return value


async def _serve() -> None:
    database_url = _environment("HAKU_E2E_DATABASE_URL")
    password = SecretStr(_environment("HAKU_E2E_BOT_PASSWORD"))
    matrix = MatrixConfig(
        homeserver=_environment("HAKU_E2E_HOMESERVER"),
        user_id=_environment("HAKU_E2E_BOT_USER_ID"),
        operator_user_id=_environment("HAKU_E2E_OPERATOR_USER_ID"),
        operator_subject="matrix-operator",
        password=password,
    )
    runtime = RuntimeRegistrationConfig(
        agent_id=UUID("00000000-0000-4000-8000-000000000001"),
        namespace="haku-claude-sandbox",
        warm_pool="haku-claude",
        claim_prefix="claude",
        runtime_label="claude-chat",
        cwd=_environment("HAKU_E2E_WORKSPACE"),
        session_ttl_seconds=7200,
        https_proxy="http://proxy.test:8180",
        ca_bundle="/egress-proxy-ca/ca-certificates.crt",
        no_proxy="127.0.0.1,localhost",
        mcp_url="http://haku-console.test:9090/mcp",
        system_prompt_template=Path(_environment("HAKU_E2E_SYSTEM_PROMPT_TEMPLATE")),
        implementation={"kind": "claude_code", "oauth_placeholder": "not-a-secret"},
    )

    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    notifications = SessionNotifications(database_url)
    await notifications.start()
    claims = FileSandboxClaims(Path(_environment("HAKU_E2E_CLAIMS_DIR")))
    runtimes = execution_registry(
        runtime_registration(
            runtime, claims, system_prompt=SystemPromptTemplate.from_path(runtime.system_prompt_template)
        )
    )
    store = SessionStore(sessions, runtimes)
    conversations = MatrixConversationStore(sessions)
    ledger = IngressLedger(sessions)
    identities = PostgresOperatorIdentityStore(
        sessions, OperatorIdentityTrust(trust_domain=TRUST_DOMAIN, trusted_issuers=frozenset({TRUSTED_ISSUER}))
    )
    sync = RefusingSyncService(
        matrix,
        password,
        engine,
        MatrixSyncStore(sessions),
        conversations,
        identities,
        MatrixTurns(matrix, store, identities, ledger),
        RoomOutbox(sessions),
        RevisionLog(sessions),
        ledger,
        RoomCopy(sessions),
        OutboxWakes(database_url),
        sessions,
        ConversationStream(sessions),
        notifications,
        armed=Path(_environment("HAKU_E2E_REFUSE_NEXT_REPLY")),
    )
    service = SessionService(runtimes, store, notifications, conversation_history=ConversationHistory(sessions))
    supervisor = ConversationRuntime(service, store, notifications, engine)
    allocator = SandboxAllocator(service, store, notifications, engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        try:
            async with sync.run(), supervisor.run(), allocator.run():
                yield
        finally:
            await service.aclose()
            await notifications.aclose()
            await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(internal_router)
    app.state.session_service = service
    config = uvicorn.Config(
        app, host="127.0.0.1", port=int(_environment("HAKU_E2E_PORT")), log_level="info", timeout_graceful_shutdown=10
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
