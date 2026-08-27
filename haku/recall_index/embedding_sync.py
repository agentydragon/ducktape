"""Fill model-specific Recall vectors from the shared content queue.

Git and chat syncs materialize source occurrences plus their exact content. This module is their
only document-embedding consumer: it drains globally de-duplicated ``contents`` rows that lack a
vector for its configured model. A model change therefore needs no source re-sync; it simply
creates a new queue view over the same durable content.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from haku.recall_index.embedder import EMBED_BATCH, Embedder
from haku.recall_index.store import ContentEmbeddingRow, insert_content_embeddings, pending_content


@dataclass(frozen=True, slots=True)
class EmbeddingSyncReport:
    """One shared queue-drain attempt."""

    contents_embedded: int


async def embed_pending(session: AsyncSession, *, embedder: Embedder, limit: int = EMBED_BATCH) -> EmbeddingSyncReport:
    """Embed at most one bounded batch of globally queued content.

    The batch is claimed ``FOR UPDATE SKIP LOCKED`` for this session's transaction, so any number
    of concurrent drains take disjoint batches. Source rows stay committed even when this fails:
    the claim dies with the transaction and a retry reads the same missing rows.
    """
    pending = await pending_content(session, model_key=embedder.model_key, limit=limit)
    if not pending:
        return EmbeddingSyncReport(contents_embedded=0)
    vectors = await embedder.embed_documents([row.content for row in pending])
    await insert_content_embeddings(
        session,
        [
            ContentEmbeddingRow(
                content_sha=row.content_sha, content=row.content, model_key=embedder.model_key, embedding=vector
            )
            for row, vector in zip(pending, vectors, strict=True)
        ],
    )
    return EmbeddingSyncReport(contents_embedded=len(pending))
