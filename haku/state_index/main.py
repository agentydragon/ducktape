"""CLI for the haku index: build each corpus, query it, report what it holds.

This is how the index gets evaluated before anything is deployed — point it at a clone of
haku-state or at a copy of the console's database, build the index, and see whether semantic
retrieval over that corpus is worth owning a service for. It talks to any Postgres with
pgvector, including a throwaway one.

The two corpora are named in every command rather than defaulted: they answer different
questions, they are built from different sources, and a query that silently searched the wrong
one would look like a retrieval quality problem.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.state_index.chat_sync import sync_chat
from haku.state_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget, git_chunker_key
from haku.state_index.git_tree import fetch_branch, open_mirror
from haku.state_index.openai_embedder import OpenAIEmbedder
from haku.state_index.store import chat_index_summary, current_git_state, ensure_schema, search_chat, search_git
from haku.state_index.sync import AlreadyCurrent, sync

app = typer.Typer(help=__doc__)

DatabaseUrl = Annotated[str, typer.Option(envvar="HAKU_STATE_INDEX_DATABASE_URL")]


def _budget() -> ChunkBudget:
    """How big a chunk gets, from the environment.

    Read here rather than passed per command because it has to be the same for indexing and for
    querying: it is part of the cache key, so a query under a different budget searches a regime
    nothing was written under and finds nothing at all.
    """
    return ChunkBudget(
        target_bytes=int(os.environ.get("HAKU_STATE_INDEX_CHUNK_TARGET_BYTES", DEFAULT_CHUNK_BUDGET.target_bytes)),
        max_bytes=int(os.environ.get("HAKU_STATE_INDEX_CHUNK_MAX_BYTES", DEFAULT_CHUNK_BUDGET.max_bytes)),
    )


def _embedder() -> OpenAIEmbedder:
    """The same embedder the console uses, so an evaluation here measures what ships.

    Point it at the cluster's Ollama (port-forward `ollama.ollama:11434`) or at one running
    locally; the model must be the one actually served, since the client fails closed on a
    mismatch rather than writing a second vector space into the corpus.
    """
    return OpenAIEmbedder(
        AsyncOpenAI(
            base_url=os.environ.get("HAKU_STATE_INDEX_EMBEDDER_URL", "http://localhost:11434/v1"), api_key="not-used"
        ),
        model=os.environ.get("HAKU_STATE_INDEX_EMBEDDER_MODEL", "qwen3-embedding:4b"),
        query_instruction=os.environ.get("HAKU_STATE_INDEX_EMBEDDER_QUERY_INSTRUCTION", ""),
    )


async def _index_git(
    database_url: str, repo_url: str, branch: str, mirror: Path, username: str | None, password: str | None
) -> None:
    engine = create_async_engine(database_url)
    embedder = _embedder()
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
                budget=_budget(),
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


@app.command("index-git")
def index_git(
    repo_url: Annotated[str, typer.Argument()],
    database_url: DatabaseUrl,
    branch: str = "main",
    mirror: Path = Path("/var/lib/haku-state-index/mirror.git"),
    username: Annotated[str | None, typer.Option(envvar="HAKU_STATE_INDEX_GIT_USERNAME")] = None,
    password: Annotated[str | None, typer.Option(envvar="HAKU_STATE_INDEX_GIT_PASSWORD")] = None,
) -> None:
    """Fetch `branch` into the mirror and swap the git index to its tip."""
    asyncio.run(_index_git(database_url, repo_url, branch, mirror, username, password))


async def _index_chat(database_url: str) -> None:
    engine = create_async_engine(database_url)
    embedder = _embedder()
    try:
        await ensure_schema(engine)
        async with async_sessionmaker(engine)() as session:
            report = await sync_chat(
                session, embedder=embedder, now=datetime.datetime.now(datetime.UTC), budget=_budget()
            )
            await session.commit()
    finally:
        await engine.dispose()
    typer.echo(
        f"{report.sessions_indexed} sessions indexed, {report.sessions_unchanged} unchanged, "
        f"{report.sessions_forgotten} forgotten; {report.windows_written} windows written "
        f"({report.windows_embedded} embedded, {report.windows_reused} reused)"
    )


@app.command("index-chat")
def index_chat(database_url: DatabaseUrl) -> None:
    """Index every chat session that has changed since it was last indexed.

    The database must be the console's own: the corpus is its `claude_chat_messages` table.
    """
    asyncio.run(_index_chat(database_url))


async def _query_git(database_url: str, query: str, limit: int, path_prefix: str | None) -> None:
    engine = create_async_engine(database_url)
    embedder = _embedder()
    try:
        async with async_sessionmaker(engine)() as session:
            hits = await search_git(
                session,
                await embedder.embed_query(query),
                chunker_key=git_chunker_key(),
                model_key=embedder.model_key,
                limit=limit,
                path_prefix=path_prefix,
            )
    finally:
        await engine.dispose()
    for hit in hits:
        preview = " ".join(hit.text.split())[:160]
        typer.echo(f"{hit.score:.3f} {hit.path}#{hit.chunk_no} [{hit.byte_start}:{hit.byte_end}] {preview}")


@app.command("query-git")
def query_git(
    text: Annotated[str, typer.Argument()], database_url: DatabaseUrl, limit: int = 10, path_prefix: str | None = None
) -> None:
    """Search the indexed git tip."""
    asyncio.run(_query_git(database_url, text, limit, path_prefix))


async def _query_chat(database_url: str, query: str, limit: int, session_id: UUID | None) -> None:
    engine = create_async_engine(database_url)
    embedder = _embedder()
    try:
        async with async_sessionmaker(engine)() as session:
            hits = await search_chat(
                session,
                await embedder.embed_query(query),
                chunker_key=git_chunker_key(),
                model_key=embedder.model_key,
                limit=limit,
                session_id=session_id,
            )
    finally:
        await engine.dispose()
    for hit in hits:
        preview = " ".join(hit.text.split())[:160]
        typer.echo(
            f"{hit.score:.3f} {hit.session_id}#{hit.chunk_no} "
            f"{hit.first_message_at:%Y-%m-%d %H:%M} +{len(hit.message_ids)} msg {preview}"
        )


@app.command("query-chat")
def query_chat(
    text: Annotated[str, typer.Argument()],
    database_url: DatabaseUrl,
    limit: int = 10,
    session_id: Annotated[UUID | None, typer.Option()] = None,
) -> None:
    """Search the indexed chat sessions."""
    asyncio.run(_query_chat(database_url, text, limit, session_id))


async def _status(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            git = await current_git_state(session)
            chat = await chat_index_summary(session)
    finally:
        await engine.dispose()
    if git is None:
        typer.echo("git: empty — nothing synced yet")
    else:
        typer.echo(
            f"git: {git.branch}@{git.commit_sha[:12]} synced {git.synced_at.isoformat()} "
            f"(chunker v{git.chunker_key}, model {git.model_key})"
        )
    if chat.last_indexed_at is None:
        typer.echo("chat: empty — nothing synced yet")
    else:
        typer.echo(
            f"chat: {chat.sessions} sessions, {chat.chunks} windows, last indexed {chat.last_indexed_at.isoformat()}"
        )


@app.command()
def status(database_url: DatabaseUrl) -> None:
    """What each corpus's searchable set currently holds."""
    asyncio.run(_status(database_url))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app()


if __name__ == "__main__":
    main()
