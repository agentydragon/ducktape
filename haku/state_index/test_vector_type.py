"""What `halfvec` accepts, which is not every float a model can return."""

from __future__ import annotations

import datetime

import pytest
import pytest_bazel
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession

from haku.state_index.schema import Corpus
from haku.state_index.store import ChunkRow, insert_chunks

_NOW = datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)


def _row(embedding: list[float], *, byte_start: int = 0) -> ChunkRow:
    return ChunkRow(
        corpus=Corpus.GIT,
        content_sha="deadbeef",
        chunker_key="k",
        model_key="m",
        byte_start=byte_start,
        byte_end=byte_start + 1,
        text="x",
        embedding=embedding,
    )


async def test_a_component_too_small_for_half_precision_rounds_to_zero(session: AsyncSession) -> None:
    """A normalized 2560-dimension embedding has components far below fp16's smallest subnormal.

    pgvector rounds them rather than refusing them, which is what makes `halfvec` usable for this
    corpus at all — worth pinning, because rejecting them would fail an insert on data no model
    promises not to produce.
    """
    await insert_chunks(session, [_row([1e-9, 0.5, -1e-12])], now=_NOW)
    await session.commit()

    stored = await session.scalar(text("SELECT embedding::text FROM state_index.chunks"))
    assert stored == "[0,0.5,-0]"


async def test_one_insert_of_a_whole_repositorys_chunks_exceeds_the_driver_limit(session: AsyncSession) -> None:
    """asyncpg caps a statement at 32767 bind parameters, and a chunk costs nine of them.

    This is what failed every haku-state sync in production on 2026-08-15: the tip produced a few
    thousand chunks, they went to Postgres as one INSERT, and the driver refused the statement
    before the database ever saw it. Batching is what avoids it; this pins the cliff so a future
    "just insert them all" cannot quietly reintroduce it.
    """
    rows = [_row([0.5, 0.5, 0.5], byte_start=index) for index in range(4000)]

    with pytest.raises(InterfaceError):
        await insert_chunks(session, rows, now=_NOW)


if __name__ == "__main__":
    pytest_bazel.main()
