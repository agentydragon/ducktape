"""CLI for the haku-state index: build it, query it, report what it holds.

This is how the index gets evaluated before anything is deployed — point it at a clone of
haku-state, build the index, and see whether semantic retrieval over that corpus is worth
owning a service for. It talks to any Postgres with pgvector, including a throwaway one.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.state_index.chunking import CHUNKER_VERSION
from haku.state_index.embedder import build_bge_small
from haku.state_index.git_tree import fetch_branch, open_mirror
from haku.state_index.store import current_state, ensure_schema, search
from haku.state_index.sync import AlreadyCurrent, sync

app = typer.Typer(help=__doc__)

DatabaseUrl = Annotated[str, typer.Option(envvar="HAKU_STATE_INDEX_DATABASE_URL")]


async def _index(
    database_url: str, repo_url: str, branch: str, mirror: Path, username: str | None, password: str | None
) -> None:
    engine = create_async_engine(database_url)
    embedder = build_bge_small()
    try:
        await ensure_schema(engine)
        repository = open_mirror(mirror, repo_url, username=username, password=password)
        commit_sha = fetch_branch(repository, branch, username=username, password=password)
        async with async_sessionmaker(engine)() as session:
            outcome = await sync(
                session,
                repository,
                commit_sha,
                branch=branch,
                embedder=embedder,
                now=datetime.datetime.now(datetime.UTC),
            )
            await session.commit()
    finally:
        await engine.dispose()
    if isinstance(outcome, AlreadyCurrent):
        typer.echo(f"{outcome.commit_sha[:12]} already indexed — nothing to do")
        return
    typer.echo(
        f"{outcome.commit_sha[:12]} {outcome.tip_files} files, {outcome.chunks_written} chunks written "
        f"({outcome.blobs_embedded} blobs embedded, {outcome.blobs_reused} reused, "
        f"{outcome.skipped_binary} binary, {outcome.skipped_large} oversized)"
    )


@app.command()
def index(
    repo_url: Annotated[str, typer.Argument()],
    database_url: DatabaseUrl,
    branch: str = "main",
    mirror: Path = Path("/var/lib/haku-state-index/mirror.git"),
    username: Annotated[str | None, typer.Option(envvar="HAKU_STATE_INDEX_GIT_USERNAME")] = None,
    password: Annotated[str | None, typer.Option(envvar="HAKU_STATE_INDEX_GIT_PASSWORD")] = None,
) -> None:
    """Fetch `branch` into the mirror and swap the index to its tip."""
    asyncio.run(_index(database_url, repo_url, branch, mirror, username, password))


async def _search(database_url: str, query: str, limit: int, path_prefix: str | None) -> None:
    engine = create_async_engine(database_url)
    embedder = build_bge_small()
    try:
        async with async_sessionmaker(engine)() as session:
            hits = await search(
                session,
                embedder.embed_query(query),
                chunker_version=CHUNKER_VERSION,
                model_key=embedder.model_key,
                limit=limit,
                path_prefix=path_prefix,
            )
    finally:
        await engine.dispose()
    for hit in hits:
        preview = " ".join(hit.text.split())[:160]
        typer.echo(f"{hit.score:.3f} {hit.path}#{hit.chunk_no} [{hit.byte_start}:{hit.byte_end}] {preview}")


@app.command()
def query(
    text: Annotated[str, typer.Argument()], database_url: DatabaseUrl, limit: int = 10, path_prefix: str | None = None
) -> None:
    """Search the indexed tip."""
    asyncio.run(_search(database_url, text, limit, path_prefix))


async def _status(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            state = await current_state(session)
    finally:
        await engine.dispose()
    if state is None:
        typer.echo("index is empty — nothing synced yet")
        return
    typer.echo(
        f"{state.branch}@{state.commit_sha[:12]} synced {state.synced_at.isoformat()} "
        f"(chunker v{state.chunker_version}, model {state.model_key})"
    )


@app.command()
def status(database_url: DatabaseUrl) -> None:
    """What the searchable set currently holds."""
    asyncio.run(_status(database_url))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app()


if __name__ == "__main__":
    main()
