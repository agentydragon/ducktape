"""The pgvector binding stores durable content embeddings correctly."""

from __future__ import annotations

import pytest_bazel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from haku.recall_index.content import content_sha
from haku.recall_index.schema import SCHEMA, Content, ContentEmbedding
from haku.recall_index.store import ContentEmbeddingRow, insert_content_embeddings


def _row(embedding: list[float]) -> ContentEmbeddingRow:
    content = "content"
    return ContentEmbeddingRow(content_sha=content_sha(content), content=content, model_key="m", embedding=embedding)


async def test_a_component_too_small_for_half_precision_rounds_to_zero(session: AsyncSession) -> None:
    """A normalized embedding may contain values below the smallest fp16 subnormal."""
    await insert_content_embeddings(session, [_row([1e-9, 0.5, -1e-12])])
    await session.commit()

    stored = await session.scalar(text(f"SELECT embedding::text FROM {SCHEMA}.content_embeddings"))
    assert stored == "[0,0.5,-0]"


async def test_one_content_value_can_have_embeddings_for_multiple_models(session: AsyncSession) -> None:
    row = _row([0.5, 0.5, 0.5])
    await insert_content_embeddings(session, [row])
    await insert_content_embeddings(
        session,
        [
            ContentEmbeddingRow(
                content_sha=row.content_sha, content=row.content, model_key="m2", embedding=[0.25, 0.25, 0.25]
            )
        ],
    )
    await session.commit()

    assert await session.scalar(select(func.count()).select_from(Content)) == 1
    assert await session.scalar(select(func.count()).select_from(ContentEmbedding)) == 2


if __name__ == "__main__":
    pytest_bazel.main()
