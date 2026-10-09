"""Run the single-index service over one branch of one Git remote."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pathspec
import pygit2
import uvicorn
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.indexing.app import create_app
from agentplane.indexing.maintenance import Maintenance
from agentplane.indexing.settings import Settings
from agentplane.indexing.source import GitSource
from agentplane.indexing.store import Store
from haku.recall_index.openai_embedder import OpenAIEmbedder

# SQLAlchemy imports the configured driver dynamically.
# gazelle:include_dep @pypi//asyncpg


async def async_main(settings: Settings) -> None:
    if settings.git_ca_bundle is not None:
        pygit2.settings.set_ssl_cert_locations(str(settings.git_ca_bundle), None)
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True, hide_parameters=True)
    try:
        store = Store(engine, budget=settings.chunk_budget, model_key=settings.embedding_model)
        await store.initialize()
        async with AsyncOpenAI(
            base_url=settings.embedding_url,
            api_key=settings.embedding_api_key.get_secret_value(),
            timeout=settings.embedding_timeout_seconds,
            max_retries=0,
        ) as embedding_client:
            embedder = OpenAIEmbedder(
                embedding_client, model=settings.embedding_model, query_instruction=settings.query_instruction
            )
            source = GitSource(
                url=settings.repository_url,
                branch=settings.branch,
                path=settings.checkout_dir,
                credentials=(
                    pygit2.UserPass(settings.git_username, settings.git_password.get_secret_value())
                    if settings.git_username is not None and settings.git_password is not None
                    else None
                ),
                ignore=pathspec.GitIgnoreSpec.from_lines(settings.ignore),
                limits=settings.snapshot_limits,
            )
            maintenance = Maintenance(
                store=store,
                source=source,
                embedder=embedder,
                poll_seconds=settings.poll_seconds,
                gc_seconds=settings.gc_seconds,
                gc_grace=timedelta(seconds=settings.gc_grace_seconds),
                embedding_timeout_seconds=settings.embedding_timeout_seconds,
            )
            app = create_app(store=store, embedder=embedder, maintenance=maintenance, read_token=settings.read_token)
            async with maintenance.run():
                await uvicorn.Server(
                    uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False)
                ).serve()
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


if __name__ == "__main__":
    main()
