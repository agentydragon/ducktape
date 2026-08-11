"""Chunking is the cache key's other half: offsets must locate the exact bytes embedded."""

from __future__ import annotations

import itertools

import pytest_bazel

from haku.state_index.chunking import _MAX_BYTES, _TARGET_BYTES, chunk_text


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
    assert all(chunk.byte_end - chunk.byte_start <= _TARGET_BYTES + 101 for chunk in chunks)


def test_single_oversized_line_is_hard_split() -> None:
    chunks = chunk_text("y" * (_MAX_BYTES * 3))
    assert len(chunks) == 3
    assert all(chunk.byte_end - chunk.byte_start <= _MAX_BYTES for chunk in chunks)


def test_whitespace_only_spans_are_dropped() -> None:
    assert chunk_text("\n\n   \n\t\n") == []


def test_empty_blob_yields_nothing() -> None:
    assert chunk_text("") == []


if __name__ == "__main__":
    pytest_bazel.main()
