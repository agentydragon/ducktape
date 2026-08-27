"""The haku-indexer worker: recall-index maintenance outside the console process.

Runs the two maintenance stages of `recall_index_sync` — source materialization and embedding —
against the deploy-owned recall-index registry in the shared console config file. The console
keeps serving `haku_index.search`/`index_status` as database readers, so this worker failing or
rolling leaves search on the last committed index state, with staleness visible in `index_status`.

Any number of replicas may run beside any number of console replicas still carrying the loop:
each logical index is maintained under its per-index Postgres advisory lock, so co-existence
costs a lost lock attempt, never a double sync.

The worker's database role is deliberately narrow — recall-index read/write plus read-only
chat-source access — and its environment holds only index Git credentials and embedder egress.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.config import EmbedderConfig, GitRecallIndexDefinition, RecallIndexSettings
from haku.console.database_migrate import sync_database_url
from haku.console.database_schema import ConversationItem
from haku.console.mcp_config import load_console_config
from haku.console.recall_index_sync import RecallEmbeddingMaintenance, RecallIndexMaintenance
from haku.recall_index.git_tree import configure_ca_trust
from haku.recall_index.openai_embedder import OpenAIEmbedder
from haku.recall_index.schema import Base as RecallIndexBase

logger = logging.getLogger(__name__)


class IndexerSettings(BaseSettings):
    """Runtime settings for the indexer worker (env-driven, prefix ``HAKU_INDEXER_``).

    Deliberately not the console's ``Settings``: the worker must be startable without operator
    OIDC, Web Push, routine, or connector credentials — requiring those here would re-grow the
    credential surface the extraction removes.
    """

    model_config = SettingsConfigDict(env_prefix="HAKU_INDEXER_", env_nested_delimiter="__")

    # The worker's narrow role, not the console's application owner.
    database_url: SecretStr
    # The shared deploy-owned console config file: `recall_indexes` is the registry of what this
    # worker maintains, and one file keeps the console's readers and this worker's writers on the
    # same registry and Git credential slots.
    config_file: Path
    # Embedding endpoint for the shared content queue. The batch loop uses the config's
    # `sync_timeout_seconds`: off the request path, waiting out a cold model load is correct.
    embedder: EmbedderConfig
    recall_index: RecallIndexSettings = Field(default_factory=RecallIndexSettings)


def verify_worker_schema(database_url: str) -> None:
    """Fail startup if this image cannot read the tables its role may touch. Never applies DDL.

    Narrower than the console's whole-metadata check on purpose: the worker's role holds only
    recall-index read/write plus read-only chat-source access, so probing any other console table
    would fail on permissions rather than on schema compatibility. An incompatible image therefore
    crash-loops here and the previous ReplicaSet keeps maintaining the index.
    """
    engine = create_engine(sync_database_url(database_url))
    try:
        with engine.connect() as conn:
            for table in RecallIndexBase.metadata.tables.values():
                conn.execute(select(table).limit(0))
            conn.execute(select(ConversationItem.__table__).limit(0))
    finally:
        engine.dispose()


async def async_main(settings: IndexerSettings) -> None:
    console_config = load_console_config(settings.config_file)
    if any(
        isinstance(index, GitRecallIndexDefinition) and index.repo_url.startswith("https://")
        for index in console_config.recall_indexes
    ):
        configure_ca_trust(console_config.git_ca_bundle)
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        maintenance = RecallIndexMaintenance(
            engine, sessions, indexes=console_config.recall_indexes, budget=settings.recall_index.chunk_budget
        )
        embedding = RecallEmbeddingMaintenance(
            engine,
            sessions,
            embedder=OpenAIEmbedder(
                AsyncOpenAI(
                    base_url=settings.embedder.base_url,
                    api_key=settings.embedder.api_key.get_secret_value(),
                    timeout=settings.embedder.sync_timeout_seconds,
                ),
                model=settings.embedder.model,
                query_instruction=settings.embedder.query_instruction,
            ),
        )
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopping.set)
        logger.info("haku-indexer maintaining %s", ", ".join(index.index_id for index in console_config.recall_indexes))
        async with maintenance.run(), embedding.run():
            await stopping.wait()
        logger.info("haku-indexer stopping")
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = IndexerSettings()
    # DDL belongs to the console's image-coupled release Job. Prove this image can read the
    # already-migrated schema before taking any advisory lock.
    verify_worker_schema(settings.database_url.get_secret_value())
    asyncio.run(async_main(settings))


if __name__ == "__main__":
    main()
