"""Run the single-index service over one branch of one Git remote."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import pathspec
import pygit2
import uvicorn
from openai import AsyncOpenAI
from pydantic import BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from haku.recall_index.openai_embedder import OpenAIEmbedder
from x.agentplane.indexing.app import create_app
from x.agentplane.indexing.maintenance import Maintenance
from x.agentplane.indexing.source import GitSource, SnapshotLimits
from x.agentplane.indexing.store import Store

# SQLAlchemy imports the configured driver dynamically.
# gazelle:include_dep @pypi//asyncpg


def _lines(value: object) -> object:
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    return value


# NoDecode keeps pydantic-settings from JSON-decoding the environment value, so a ConfigMap
# can hold the patterns one per line exactly as a .gitignore would.
IgnorePatterns = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_lines)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_INDEX_", cli_parse_args=True, cli_kebab_case=True)

    database_url: SecretStr = Field(description="Dedicated PostgreSQL database, postgresql+asyncpg:// URL.")
    repository_url: str = Field(description="Git remote to index: an HTTP(S) URL or a local path.")
    branch: str
    checkout_dir: Path = Field(description="Directory for the bare clone; created on first use.")
    git_username: str | None = None
    git_password: SecretStr | None = None
    git_ca_bundle: Path | None = Field(
        default=None, description="PEM bundle for HTTPS remotes; libgit2 does not read SSL_CERT_FILE."
    )
    ignore: IgnorePatterns = Field(
        default=(), description="gitignore-syntax patterns, one per line; matching paths are left out of snapshots."
    )
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
    snapshot_limits: SnapshotLimits = SnapshotLimits()
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    @model_validator(mode="after")
    def _paired_credentials(self) -> Settings:
        if (self.git_username is None) != (self.git_password is None):
            raise ValueError("git_username and git_password must be set together")
        return self

    @property
    def git_credentials(self) -> pygit2.UserPass | None:
        if self.git_username is None or self.git_password is None:
            return None
        return pygit2.UserPass(self.git_username, self.git_password.get_secret_value())


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
                credentials=settings.git_credentials,
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
