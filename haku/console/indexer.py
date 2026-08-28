"""The haku-indexer worker: recall-index maintenance outside the console process.

One binary, two roles selected by ``--role``, each running one maintenance stage of
`recall_index_sync` in its own Deployment:

- ``chunk`` materializes the indexes its mounted config file defines. One Deployment per logical
  index (#4886), each mounting only its own index's slice of the deploy-owned registry — the
  mounted config *is* the selection, so a malformed definition for one index can never parse-break
  another index's pod. Replicas of an index's pod, and console replicas of a release that still
  carried the whole-registry loop, may overlap freely: each logical index is maintained under its
  per-index Postgres advisory lock, so co-existence costs a lost lock attempt, never a double sync.
- ``embed`` drains the shared embedding queue. Replicas share the queue in disjoint batches
  (`embed_pending` claims ``FOR UPDATE SKIP LOCKED``), so overlap scales the drain instead of
  double-embedding.

The console keeps serving `haku_index.search`/`index_status` as database readers, so either role
failing or rolling leaves search on the last committed index state, with staleness visible in
`index_status`.

Each role's settings model requires only that role's configuration — a chunk pod holds its own
index's config slice and Git credential (none for the chat or anonymous-Git index) and no embedder
endpoint, the embed pod holds the embedder endpoint and no Git credential or config file — so a pod
cannot start with the other role's secrets missing *or* present-but-unused.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.database_schema import ConversationItem
from haku.console.indexer_config import load_indexer_config
from haku.console.recall_index_sync import RecallEmbeddingMaintenance, RecallIndexMaintenance
from haku.recall_index.config import EmbedderConfig, GitRecallIndexDefinition, RecallIndexSettings
from haku.recall_index.git_tree import configure_ca_trust
from haku.recall_index.openai_embedder import OpenAIEmbedder
from haku.recall_index.schema import Base as RecallIndexBase

logger = logging.getLogger(__name__)


class IndexerRole(StrEnum):
    """Which maintenance stage this process runs."""

    CHUNK = "chunk"
    EMBED = "embed"


class _WorkerSettings(BaseSettings):
    """Env-driven settings shared by both roles (prefix ``HAKU_INDEXER_``).

    Deliberately not the console's ``Settings``: the worker must be startable without operator
    OIDC, Web Push, routine, or connector credentials — requiring those here would re-grow the
    credential surface the extraction removes.
    """

    model_config = SettingsConfigDict(env_prefix="HAKU_INDEXER_", env_nested_delimiter="__")

    # The worker's narrow role, not the console's application owner.
    database_url: SecretStr


class ChunkSettings(_WorkerSettings):
    """The chunk role: sweep the indexes the mounted config file defines into chunks.

    One instance, one config slice (#4886): each per-index Deployment mounts only its own index's
    definition, so the mounted config is the selection — no selector setting — and a pod carries
    only that index's credential (the chat and anonymous-Git indexes carry no Git slot). Overlap
    between the per-index pods, and with a rolling old whole-registry release, stays safe under the
    per-logical-index advisory lock.
    """

    # The pod's own slice of the deploy-owned registry, derivation-tested against the console's
    # `recall_indexes` (test_deployment_config) so the console's readers and this role's writers
    # stay on the same registry entry and Git credential slots.
    config_file: Path
    recall_index: RecallIndexSettings = Field(default_factory=RecallIndexSettings)


class EmbedSettings(_WorkerSettings):
    """The embed role: drain the shared content queue into one model's vector space."""

    # Embedding endpoint for the shared content queue. The batch loop uses the config's
    # `sync_timeout_seconds`: off the request path, waiting out a cold model load is correct.
    embedder: EmbedderConfig


def _sync_database_url(database_url: str) -> str:
    """Render the async application URL for the synchronous psycopg schema probe.

    Deliberately duplicates ``database_migrate.sync_database_url`` rather than importing it: the
    import would carry the whole console ORM into the worker (<docs/naming_and_layout.md> §5).
    """
    return make_url(database_url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def verify_worker_schema(database_url: str) -> None:
    """Fail startup if this image cannot read the tables its role may touch. Never applies DDL.

    Narrower than the console's whole-metadata check on purpose: both process roles run as the
    one `haku_indexer` database role, which holds only recall-index read/write plus read-only
    chat-source access, so probing any other console table would fail on permissions rather than
    on schema compatibility. An incompatible image therefore crash-loops here and the previous
    ReplicaSet keeps maintaining the index.
    """
    engine = create_engine(_sync_database_url(database_url))
    try:
        with engine.connect() as conn:
            for table in RecallIndexBase.metadata.tables.values():
                conn.execute(select(table).limit(0))
            conn.execute(select(ConversationItem.__table__).limit(0))
    finally:
        engine.dispose()


async def async_main(settings: ChunkSettings | EmbedSettings) -> None:
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        stage: AbstractAsyncContextManager[None]
        if isinstance(settings, ChunkSettings):
            config = load_indexer_config(settings.config_file)
            if any(
                isinstance(index, GitRecallIndexDefinition) and index.repo_url.startswith("https://")
                for index in config.recall_indexes
            ):
                configure_ca_trust(config.git_ca_bundle)
            stage = RecallIndexMaintenance(
                engine, sessions, indexes=config.recall_indexes, budget=settings.recall_index.chunk_budget
            ).run()
            logger.info("haku-indexer chunking %s", ", ".join(index.index_id for index in config.recall_indexes))
        else:
            stage = RecallEmbeddingMaintenance(
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
            ).run()
            logger.info("haku-indexer embedding for model %s", settings.embedder.model)
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopping.set)
        async with stage:
            await stopping.wait()
        logger.info("haku-indexer stopping")
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="haku-indexer recall-index maintenance worker")
    parser.add_argument(
        "--role",
        type=IndexerRole,
        choices=tuple(IndexerRole),
        required=True,
        help="chunk: sweep configured sources into chunks; embed: drain the shared embedding queue",
    )
    role: IndexerRole = parser.parse_args().role
    settings = ChunkSettings() if role is IndexerRole.CHUNK else EmbedSettings()
    # DDL belongs to the console's image-coupled release Job. Prove this image can read the
    # already-migrated schema before doing any maintenance work.
    verify_worker_schema(settings.database_url.get_secret_value())
    asyncio.run(async_main(settings))


if __name__ == "__main__":
    main()
