"""Chunking is the cache key's other half: offsets must locate the exact bytes embedded."""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict

import pytest
import pytest_bazel

from haku.state_index.chunking import CHUNKER_VERSION, DEFAULT_CHUNK_BUDGET, ChunkBudget, chunk_text, git_chunker_key


def test_offsets_slice_the_embedded_text_back_out() -> None:
    blob = "\n".join(f"line {n} with some words in it" for n in range(200))
    encoded = blob.encode()
    for chunk in chunk_text(blob):
        assert encoded[chunk.byte_start : chunk.byte_end].decode() == chunk.text


def test_offsets_are_correct_for_multibyte_content() -> None:
    blob = "první řádek s háčky\n" * 200
    encoded = blob.encode()
    for chunk in chunk_text(blob):
        assert encoded[chunk.byte_start : chunk.byte_end].decode() == chunk.text


def test_chunks_are_contiguous_and_numbered_in_order() -> None:
    chunks = chunk_text("\n".join(f"line {n}" for n in range(500)))
    assert [chunk.chunk_no for chunk in chunks] == list(range(len(chunks)))
    assert all(a.byte_end == b.byte_start for a, b in itertools.pairwise(chunks))


def test_packs_up_to_the_target_without_splitting_lines() -> None:
    chunks = chunk_text("\n".join("x" * 100 for _ in range(100)))
    assert len(chunks) > 1
    # Each chunk may exceed the target by at most the line that tipped it over.
    assert all(chunk.byte_end - chunk.byte_start <= DEFAULT_CHUNK_BUDGET.target_bytes + 101 for chunk in chunks)


def test_single_oversized_line_is_hard_split() -> None:
    chunks = chunk_text("y" * (DEFAULT_CHUNK_BUDGET.max_bytes * 3))
    assert len(chunks) == 3
    assert all(chunk.byte_end - chunk.byte_start <= DEFAULT_CHUNK_BUDGET.max_bytes for chunk in chunks)


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


def test_the_budget_is_part_of_what_identifies_a_chunk() -> None:
    """Otherwise re-tuning it would serve vectors computed over spans that no longer exist."""
    assert git_chunker_key(ChunkBudget(target_bytes=200, max_bytes=400)) != git_chunker_key(DEFAULT_CHUNK_BUDGET)


def test_the_key_carries_every_field_of_the_budget() -> None:
    """The reason it is serialized rather than formatted: a field added to `ChunkBudget` lands in
    the key without anyone remembering to put it there."""
    assert json.loads(git_chunker_key(DEFAULT_CHUNK_BUDGET)) == {
        "version": CHUNKER_VERSION,
        **asdict(DEFAULT_CHUNK_BUDGET),
    }


def test_the_key_is_canonical() -> None:
    """Same regime, same bytes — in this process and in the one that wrote the row."""
    assert git_chunker_key(ChunkBudget(target_bytes=1, max_bytes=2)) == '{"max_bytes":2,"target_bytes":1,"version":1}'


def test_a_budget_that_cannot_be_satisfied_is_refused() -> None:
    with pytest.raises(ValueError, match="nonsensical"):
        ChunkBudget(target_bytes=4000, max_bytes=100)


if __name__ == "__main__":
    pytest_bazel.main()
