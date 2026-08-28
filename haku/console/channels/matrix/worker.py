"""The haku-matrix-adapter worker: the Matrix channel outside the console process.

One binary, one loop: the sync service (`sync.py`) with its per-attachment reconcilers, contending
for the same `MXSY` advisory lock the in-console loop held — so during the extraction roll a
console replica of the previous release and this worker cannot double-process a batch, only lose
an election. It holds the Matrix credential the console pod no longer carries, and it speaks to
the conversation layer only through the public seam: the `conversation_wakes` channel, the
positional read (`x/subscription.py`), and the offer-input port.

Its settings model is the pod's contract (the indexer pattern): the worker cannot start without
exactly its database role, the shared config file, and the Matrix wiring including the bot
credential — and starts with nothing else. The launch-identity registry it reads from the shared
file is `config.AdapterConfigFile`, and its authority is the narrow
`agents/launch_authority.StaticLaunchAuthority`, so the binary's dependencies stay clear of the
console's MCP/auth stack (<../../docs/naming_and_layout.md> §5).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from more_itertools import one
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.agents.launch_authority import StaticLaunchAuthority
from haku.console.channels.matrix.config import AdapterConfigFile, Config, load_adapter_config
from haku.console.channels.matrix.conversation import ConversationStore, Turns
from haku.console.channels.matrix.ingress_ledger import IngressLedger
from haku.console.channels.matrix.outbox import RoomOutbox
from haku.console.channels.matrix.outbox_wake import OutboxWakes
from haku.console.channels.matrix.revisions import RevisionLog
from haku.console.channels.matrix.room_copy import RoomCopy
from haku.console.channels.matrix.sync import SyncService, SyncStore
from haku.console.chat_models import RuntimeKind
from haku.console.database_schema import (
    Agent,
    ChannelAttachmentRow,
    ChannelCursor,
    Conversation,
    ConversationEventRow,
    ConversationItem,
    ConversationPrompt,
    ConversationTurn,
    CredentialBinding,
    IdentityAnchor,
    MatrixAccessToken,
    MatrixIngressEvent,
    MatrixOutbox,
    MatrixRevision,
    MatrixRoomCopy,
    MatrixSyncWatermark,
    Operator,
    Session,
    StaticCredential,
    SubmittedPrompt,
)
from haku.console.notifications.conversation_wakes import ConversationWakes
from haku.console.operator_identity import OperatorIdentityTrust
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.session.launch_identity import ChatLaunchAuthorizer
from haku.console.session.store import Store
from haku.console.session.subscription import ConversationStream
from haku.console.x.runtime import RuntimeKey, RuntimeRegistry

logger = logging.getLogger(__name__)


class AdapterSettings(BaseSettings):
    """Env-driven settings (prefix ``HAKU_MATRIX_ADAPTER_``).

    Deliberately not the console's ``Settings``: the worker must be startable without operator
    OIDC, Web Push, routine, or connector credentials — requiring those here would re-grow the
    credential surface the extraction removes.
    """

    model_config = SettingsConfigDict(env_prefix="HAKU_MATRIX_ADAPTER_", env_nested_delimiter="__")

    # The worker's narrow role, not the console's application owner.
    database_url: SecretStr
    # The shared deploy-owned console config file: the launch-identity registry a room bind
    # consults. One mounted file keeps this worker's binds and the console's own launches
    # selecting the same Agents and profiles.
    config_file: Path
    matrix: Config
    # Must equal the console's `HAKU_CONSOLE_OPERATOR_IDENTITY__TRUST_DOMAIN`: the anchor rows the
    # configured operator subject resolves through are keyed by it, and a differing value would
    # resolve the sender into a namespace no console login ever wrote.
    operator_identity_trust_domain: str


# Every table the narrow `haku_matrix_adapter` role may touch (matrix-adapter-role.sql): the
# channel's own state, the conversation seam, and the identity/authority rows a bind reads.
_ROLE_TABLES = (
    MatrixAccessToken,
    MatrixSyncWatermark,
    MatrixRevision,
    MatrixRoomCopy,
    MatrixOutbox,
    MatrixIngressEvent,
    ChannelAttachmentRow,
    ChannelCursor,
    Conversation,
    ConversationEventRow,
    ConversationItem,
    ConversationTurn,
    ConversationPrompt,
    SubmittedPrompt,
    Session,
    Operator,
    IdentityAnchor,
    Agent,
    CredentialBinding,
    StaticCredential,
)


def _sync_database_url(database_url: str) -> str:
    """Render the async application URL for the synchronous psycopg schema probe.

    Deliberately duplicates ``database_migrate.sync_database_url`` rather than importing it: the
    import would carry the whole console ORM metadata check into the worker
    (<../../docs/naming_and_layout.md> §5).
    """
    return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def verify_worker_schema(database_url: str) -> None:
    """Fail startup if this image cannot read the tables its role may touch. Never applies DDL.

    Narrower than the console's whole-metadata check on purpose: the worker runs as the
    `haku_matrix_adapter` role, so probing any other console table would fail on permissions
    rather than on schema compatibility. An incompatible image crash-loops here and the previous
    ReplicaSet keeps serving the rooms.
    """
    engine = create_engine(_sync_database_url(database_url))
    try:
        with engine.connect() as conn:
            for table in _ROLE_TABLES:
                conn.execute(select(table.__table__).limit(0))
    finally:
        engine.dispose()


@dataclass(frozen=True, slots=True)
class LaunchWiring:
    """What a first bind stamps: the authorizer and the default identity it authorizes."""

    authorizer: ChatLaunchAuthorizer
    default_agent_id: UUID
    default_runtime_kind: RuntimeKind


def _launch_wiring(config: AdapterConfigFile) -> LaunchWiring | None:
    """The launch-identity wiring a first bind stamps, from the shared registry.

    Mirrors the console's own composition (`app.create_app`): the registered Agent/runtime pairs
    come from the configured harnesses, the profile gates from `access_profiles`, and the default
    selection prefers Claude among the default Agent's admitted kinds. None where no harness is
    configured — a bind then opens the conversation without a launch identity, exactly as a
    launch-incapable console did.
    """
    if config.harnesses is None:
        return None
    registered = {
        RuntimeKey(entry.agent_id, kind)
        for entry, kind in (
            (config.harnesses.claude_code, RuntimeKind.CLAUDE_CODE),
            (config.harnesses.codex_app_server, RuntimeKind.CODEX_APP_SERVER),
        )
        if entry is not None
    }
    if not registered:
        return None
    if config.default_chat_agent_id is None:
        raise ValueError("harnesses are configured but default_chat_agent_id is not")
    static_by_id = {agent.agent_id: agent for agent in config.static_agents}
    profile_runtime_kinds = {profile.id: profile.allowed_chat_runtimes for profile in config.access_profiles}
    authorizer = ChatLaunchAuthorizer(
        StaticLaunchAuthority(),
        launchable_agent_ids={entry.agent_id for entry in config.launchable_agents},
        registered_runtime_identities=registered,
        profile_runtime_kinds=profile_runtime_kinds,
    )
    default_profile_id = static_by_id[config.default_chat_agent_id].access_profile_id
    default_candidates = {
        identity.runtime_kind
        for identity in registered
        if identity.agent_id == config.default_chat_agent_id
        and identity.runtime_kind in profile_runtime_kinds[default_profile_id]
    }
    if RuntimeKind.CLAUDE_CODE in default_candidates:
        default_runtime_kind = RuntimeKind.CLAUDE_CODE
    else:
        try:
            default_runtime_kind = one(default_candidates)
        except ValueError:
            raise ValueError("default chat Agent must select one configured runtime") from None
    return LaunchWiring(authorizer, config.default_chat_agent_id, default_runtime_kind)


async def async_main(settings: AdapterSettings) -> None:
    database_url = settings.database_url.get_secret_value()
    engine = create_async_engine(database_url, pool_pre_ping=True)
    conversation_wakes = ConversationWakes(database_url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        launch = _launch_wiring(load_adapter_config(settings.config_file))
        conversations = ConversationStore(sessions)
        if launch is not None:
            conversations.configure_launch_identity(
                launch.authorizer,
                default_agent_id=launch.default_agent_id,
                default_runtime_kind=launch.default_runtime_kind,
            )
        identities = PostgresOperatorIdentityStore(
            sessions,
            # The worker never verifies OIDC principals, so it trusts no issuer; it only resolves
            # the configured subject inside the console's anchor namespace.
            OperatorIdentityTrust(trust_domain=settings.operator_identity_trust_domain, trusted_issuers=frozenset()),
        )
        ledger = IngressLedger(sessions)
        # The registry parameterizes frame projection, which only a session's single writer runs;
        # the offer-input path this worker calls never dispatches a harness, so it stays empty and
        # the binary links no harness adapter.
        session_store = Store(sessions, RuntimeRegistry({}))
        sync_service = SyncService(
            settings.matrix,
            engine,
            SyncStore(sessions),
            conversations,
            identities,
            Turns(settings.matrix, session_store, identities, ledger),
            RoomOutbox(sessions),
            RevisionLog(sessions),
            ledger,
            RoomCopy(sessions),
            OutboxWakes(database_url),
            sessions,
            ConversationStream(sessions),
            conversation_wakes,
        )
        await conversation_wakes.start()
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopping.set)
        async with sync_service.run():
            logger.info("haku-matrix-adapter serving %s as %s", settings.matrix.homeserver, settings.matrix.user_id)
            await stopping.wait()
        logger.info("haku-matrix-adapter stopping")
    finally:
        await conversation_wakes.aclose()
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = AdapterSettings()
    # DDL belongs to the console's image-coupled release Job. Prove this image can read the
    # already-migrated schema before logging in to the homeserver.
    verify_worker_schema(settings.database_url.get_secret_value())
    asyncio.run(async_main(settings))


if __name__ == "__main__":
    main()
