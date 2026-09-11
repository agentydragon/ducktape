"""Run the loosely coupled, single-index Flux GitRepository service."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, CustomObjectsApi
from openai import AsyncOpenAI
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.openai_embedder import OpenAIEmbedder
from util.kubernetes import CustomObjectsClient
from x.agentplane.indexing.app import create_app
from x.agentplane.indexing.maintenance import Maintenance
from x.agentplane.indexing.source import ArchiveLimits, FluxSource
from x.agentplane.indexing.store import Store

# SQLAlchemy imports the configured driver dynamically.
# gazelle:include_dep @pypi//asyncpg


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_INDEX_", cli_parse_args=True, cli_kebab_case=True)

    database_url: SecretStr = Field(description="Dedicated PostgreSQL database, postgresql+asyncpg:// URL.")
    source_namespace: str
    source_name: str
    embedding_url: str
    embedding_model: str
    embedding_api_key: SecretStr
    read_token: SecretStr = Field(min_length=1, description="Bearer credential required for search and status.")
    query_instruction: str = ""
    embedding_timeout_seconds: float = Field(default=60, gt=0)
    poll_seconds: float = Field(default=30, gt=0)
    gc_seconds: float = Field(default=3600, gt=0)
    gc_grace_seconds: float = Field(default=86400, ge=0)
    chunk_budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
    archive_limits: ArchiveLimits = ArchiveLimits()
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    kubeconfig: Path | None = None

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)


async def async_main(settings: Settings) -> None:
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True, hide_parameters=True)
    try:
        store = Store(engine, budget=settings.chunk_budget, model_key=settings.embedding_model)
        await store.initialize()
        async with (
            ApiClient(configuration=configuration) as api,
            httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as http,
            AsyncOpenAI(
                base_url=settings.embedding_url,
                api_key=settings.embedding_api_key.get_secret_value(),
                timeout=settings.embedding_timeout_seconds,
                max_retries=0,
            ) as embedding_client,
        ):
            embedder = OpenAIEmbedder(
                embedding_client, model=settings.embedding_model, query_instruction=settings.query_instruction
            )
            source = FluxSource(
                custom_objects=cast(CustomObjectsClient, CustomObjectsApi(api)),
                http=http,
                namespace=settings.source_namespace,
                name=settings.source_name,
                limits=settings.archive_limits,
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
