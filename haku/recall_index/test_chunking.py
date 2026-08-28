"""Chunking chooses code-point-safe boundaries and records byte-exact Git provenance."""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict

import pytest
import pytest_bazel

from haku.recall_index.chunking import (
    CHUNKER_VERSION,
    DEFAULT_CHUNK_BUDGET,
    ChunkBudget,
    chunk_text,
    git_chunker_key,
    split_utf8,
)


def test_offsets_slice_the_embedded_text_back_out() -> None:
    blob = "\n".join(f"line {n} with some words in it" for n in range(200))
    encoded = blob.encode()
    for chunk in chunk_text(blob):
        assert encoded[chunk.byte_start : chunk.byte_end].decode() == chunk.text


def test_overlap_is_code_point_safe_for_multibyte_content() -> None:
    blob = "první řádek 😀\n" * 200
    encoded = blob.encode()
    chunks = chunk_text(blob, ChunkBudget(target_bytes=180, max_bytes=360, overlap_codepoints=12))
    assert len(chunks) > 1
    for chunk in chunks:
        assert encoded[chunk.byte_start : chunk.byte_end].decode() == chunk.text
    assert all(left.text[-12:] == right.text[:12] for left, right in itertools.pairwise(chunks))


def test_zero_overlap_keeps_chunks_contiguous_and_in_document_order() -> None:
    chunks = chunk_text(
        "\n".join(f"line {n}" for n in range(500)), ChunkBudget(target_bytes=100, max_bytes=200, overlap_codepoints=0)
    )
    assert all(left.byte_end == right.byte_start for left, right in itertools.pairwise(chunks))


def test_packs_to_preferred_line_boundaries() -> None:
    chunks = chunk_text("\n".join("x" * 100 for _ in range(100)))
    assert len(chunks) > 1
    # Each ordinary chunk ends at a line boundary and stays within the hard byte cap.
    assert all(chunk.text.endswith("\n") or chunk is chunks[-1] for chunk in chunks)
    assert all(chunk.byte_end - chunk.byte_start <= DEFAULT_CHUNK_BUDGET.max_bytes for chunk in chunks)


def test_single_oversized_line_is_hard_split_at_code_point_boundaries() -> None:
    budget = ChunkBudget(target_bytes=100, max_bytes=200, overlap_codepoints=10)
    blob = "😀" * 200
    chunks = chunk_text(blob, budget)
    assert len(chunks) > 1
    assert all(chunk.byte_end - chunk.byte_start <= budget.max_bytes for chunk in chunks)
    assert all(left.text[-10:] == right.text[:10] for left, right in itertools.pairwise(chunks))
    assert chunks[0].text.startswith(blob[:1])
    assert chunks[-1].text.endswith(blob[-1:])


def test_whitespace_only_spans_are_dropped() -> None:
    assert chunk_text("\n\n   \n\t\n") == []


def test_empty_blob_yields_nothing() -> None:
    assert chunk_text("") == []


def test_a_budget_packs_to_its_own_size() -> None:
    """The size is a parameter, so the same text chunks differently under a different budget."""
    blob = "".join(f"line {index}\n" for index in range(400))
    small = chunk_text(blob, ChunkBudget(target_bytes=200, max_bytes=400))
    large = chunk_text(blob, ChunkBudget(target_bytes=4000, max_bytes=8000))
    assert len(small) > len(large)
    assert all(chunk.byte_end - chunk.byte_start <= 400 for chunk in small)


def test_overlap_is_part_of_what_identifies_a_retrieval_regime() -> None:
    budget = ChunkBudget(target_bytes=200, max_bytes=400)
    assert git_chunker_key(budget) != git_chunker_key(
        ChunkBudget(target_bytes=200, max_bytes=400, overlap_codepoints=32)
    )


def test_the_key_carries_every_field_of_the_budget() -> None:
    """Serializing the budget means a field lands in the key automatically."""
    assert json.loads(git_chunker_key(DEFAULT_CHUNK_BUDGET)) == {
        "version": CHUNKER_VERSION,
        **asdict(DEFAULT_CHUNK_BUDGET),
    }


def test_the_key_is_canonical() -> None:
    assert git_chunker_key(ChunkBudget(target_bytes=1, max_bytes=2)) == (
        '{"max_bytes":2,"overlap_codepoints":0,"target_bytes":1,"version":2}'
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"target_bytes": 4000, "max_bytes": 100}, "nonsensical"),
        ({"target_bytes": 100, "max_bytes": 400, "overlap_codepoints": -1}, "negative"),
        ({"target_bytes": 100, "max_bytes": 400, "overlap_codepoints": 100}, "no room"),
    ],
)
def test_a_budget_that_cannot_be_satisfied_is_refused(kwargs: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ChunkBudget(**kwargs)


def test_split_utf8_uses_code_point_overlap() -> None:
    spans = list(split_utf8("😀" * 100, 40, overlap_codepoints=3))
    assert all(left.text[-3:] == right.text[:3] for left, right in itertools.pairwise(spans))


if __name__ == "__main__":
    pytest_bazel.main()
