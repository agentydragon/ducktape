"""Bring the index up to a branch tip.

The unit of work is a blob, not a file: the same content at two paths (or moved between
them) is embedded once, and content already embedded under this (chunker, model) regime is
never embedded again. Everything the sync writes lands in the caller's transaction, so a
failed run — an embedder that died halfway, a lost connection — leaves the previous tip
searchable rather than a half-swapped one.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import pygit2
from more_itertools import batched
from sqlalchemy.ext.asyncio import AsyncSession

from haku.state_index.chunking import DEFAULT_CHUNK_BUDGET, Chunk, ChunkBudget, chunk_text, git_chunker_key
from haku.state_index.embedder import Embedder
from haku.state_index.git_tree import list_tip, read_blob
from haku.state_index.schema import Corpus, GitSyncState
from haku.state_index.store import (
    ChunkRow,
    cached_content,
    current_git_state,
    insert_chunks,
    replace_tip,
    touch_content,
)

logger = logging.getLogger(__name__)

# Above this, a blob is data rather than prose: a checked-in binary, a lockfile, a dump. It
# stays in `tip` (the tip is the tree, honestly reported) but is never chunked, so it simply
# never matches.
MAX_BLOB_BYTES = 1 << 20

_EMBED_BATCH = 32


@dataclass(frozen=True, slots=True)
class SyncReport:
    commit_sha: str
    tip_files: int
    blobs_embedded: int
    blobs_reused: int
    chunks_written: int
    skipped_binary: int
    skipped_large: int


@dataclass(frozen=True, slots=True)
class AlreadyCurrent:
    """The index already holds this commit under this regime, so the sync did nothing."""

    commit_sha: str


# A variant rather than a flag on SyncReport: the counts an early-out would report are all
# absent, not zero, and callers should have to notice the difference.
SyncOutcome = SyncReport | AlreadyCurrent


def is_current(
    state: GitSyncState | None,
    commit_sha: str,
    *,
    branch: str,
    model_key: str,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> bool:
    """Whether the searchable set already holds this commit under this regime.

    Public because a caller polling the remote needs exactly this question before deciding to
    fetch, and answering it a second time by hand is how a regime change gets skipped: the tip
    has not moved, so a commit-only comparison says "nothing to do" while the stored vectors no
    longer answer for the content.
    """
    return state is not None and (state.commit_sha, state.branch, state.chunker_key, state.model_key) == (
        commit_sha,
        branch,
        git_chunker_key(budget),
        model_key,
    )


async def sync(
    session: AsyncSession,
    repo: pygit2.Repository,
    commit_sha: str,
    *,
    branch: str,
    embedder: Embedder,
    now: datetime.datetime,
    budget: ChunkBudget = DEFAULT_CHUNK_BUDGET,
) -> SyncOutcome:
    """Swap the searchable set to `commit_sha`, or do nothing if it is already there.

    The early-out compares the whole regime, not just the commit: a different chunker or
    embedding model means the stored vectors no longer answer for this content even though
    the tree is identical. It exists so a push-triggered sync and a reconciling cron can both
    fire as often as they like — the common case, where nothing moved, costs one SELECT.
    """
    regime = git_chunker_key(budget)
    if is_current(
        await current_git_state(session), commit_sha, branch=branch, model_key=embedder.model_key, budget=budget
    ):
        logger.info("haku-state index already at %s", commit_sha)
        return AlreadyCurrent(commit_sha=commit_sha)

    entries = list_tip(repo, commit_sha)
    blob_shas = {entry.blob_sha for entry in entries}
    cached = await cached_content(
        session, Corpus.GIT, sorted(blob_shas), chunker_key=regime, model_key=embedder.model_key
    )

    pending: list[tuple[str, Chunk]] = []
    skipped_binary = 0
    skipped_large = 0
    for blob_sha in sorted(blob_shas - cached):
        data = read_blob(repo, blob_sha)
        if len(data) > MAX_BLOB_BYTES:
            skipped_large += 1
            continue
        try:
            blob_text = data.decode()
        except UnicodeDecodeError:
            skipped_binary += 1
            continue
        pending.extend((blob_sha, chunk) for chunk in chunk_text(blob_text, budget))

    rows: list[ChunkRow] = []
    for batch in batched(pending, _EMBED_BATCH):
        vectors = await embedder.embed_documents([chunk.text for _, chunk in batch])
        rows.extend(
            ChunkRow(
                corpus=Corpus.GIT,
                content_sha=blob_sha,
                chunk_no=chunk.chunk_no,
                chunker_key=regime,
                model_key=embedder.model_key,
                byte_start=chunk.byte_start,
                byte_end=chunk.byte_end,
                text=chunk.text,
                embedding=vector,
            )
            for (blob_sha, chunk), vector in zip(batch, vectors, strict=True)
        )

    await insert_chunks(session, rows, now=now)
    await touch_content(session, Corpus.GIT, sorted(cached), chunker_key=regime, model_key=embedder.model_key, now=now)
    await replace_tip(
        session,
        entries,
        commit_sha=commit_sha,
        branch=branch,
        chunker_key=regime,
        model_key=embedder.model_key,
        now=now,
    )
    report = SyncReport(
        commit_sha=commit_sha,
        tip_files=len(entries),
        blobs_embedded=len({blob_sha for blob_sha, _ in pending}),
        blobs_reused=len(cached),
        chunks_written=len(rows),
        skipped_binary=skipped_binary,
        skipped_large=skipped_large,
    )
    logger.info("synced haku-state index: %s", report)
    return report
