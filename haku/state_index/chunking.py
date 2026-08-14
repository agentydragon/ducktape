"""Deterministic chunking of a text blob into embeddable spans.

Chunk boundaries are part of the embedding cache key (`CHUNKER_VERSION`), so any change to
the algorithm below must bump that constant — otherwise cached vectors from the previous
regime are silently reused for text they were never computed over.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

# Bump on ANY behavioral change here (targets, boundary rules).
CHUNKER_VERSION = 1

# bge-small truncates at 512 tokens (~2 KiB of English prose), so a target well under that
# keeps whole chunks inside the model's window; the max is the hard split for a single
# unbroken line (minified JSON, a base64 blob) that the line rule cannot divide.
_TARGET_BYTES = 1500
_MAX_BYTES = 3000


@dataclass(frozen=True, slots=True)
class Chunk:
    """One embeddable span of a blob, located by byte offsets into that blob."""

    chunk_no: int
    byte_start: int
    byte_end: int
    text: str


@dataclass(frozen=True, slots=True)
class Span:
    """A run of text and where it sits, in bytes, inside the document it came from."""

    byte_start: int
    byte_end: int
    text: str


def split_utf8(text: str, max_bytes: int, *, byte_start: int = 0, size: int | None = None) -> Iterator[Span]:
    """Hard-split `text` into spans of at most `max_bytes`, never inside a character.

    The chat chunker shares this for the one case its message-boundary packing cannot handle —
    a single message longer than a chunk — so both corpora split unbreakable text the same way.
    Pass `size` when the caller already knows the encoded length; the common case is a span that
    fits, and re-encoding to find that out is the whole cost of chunking a large file.
    """
    encoded_size = len(text.encode()) if size is None else size
    if encoded_size <= max_bytes:
        yield Span(byte_start=byte_start, byte_end=byte_start + encoded_size, text=text)
        return
    offset = byte_start
    buffer = ""
    for char in text:
        if len((buffer + char).encode()) > max_bytes:
            encoded = len(buffer.encode())
            yield Span(byte_start=offset, byte_end=offset + encoded, text=buffer)
            offset += encoded
            buffer = ""
        buffer += char
    if buffer:
        yield Span(byte_start=offset, byte_end=offset + len(buffer.encode()), text=buffer)


def _lines(blob: str) -> Iterator[Span]:
    """Split into lines that keep their terminators, carrying byte offsets into the blob."""
    offset = 0
    for line in blob.splitlines(keepends=True):
        end = offset + len(line.encode())
        yield Span(byte_start=offset, byte_end=end, text=line)
        offset = end


def _pack(lines: Iterator[Span]) -> Iterator[list[Span]]:
    """Greedily pack lines into groups of at most the target size, never splitting a line."""
    group: list[Span] = []
    for line in lines:
        span = line.byte_end - line.byte_start
        if group and (group[-1].byte_end - group[0].byte_start) + span > _TARGET_BYTES:
            yield group
            group = []
        group.append(line)
    if group:
        yield group


def chunk_text(blob: str) -> list[Chunk]:
    """Chunk a decoded blob into embeddable spans.

    Blank-only spans are dropped: they embed to noise and would only ever be retrieved by
    accident. Offsets are byte offsets into the blob as stored in git, so a caller holding
    the blob can slice the exact span back out.
    """
    chunks: list[Chunk] = []
    for group in _pack(
        part
        for line in _lines(blob)
        for part in split_utf8(line.text, _MAX_BYTES, byte_start=line.byte_start, size=line.byte_end - line.byte_start)
    ):
        text = "".join(line.text for line in group)
        if not text.strip():
            continue
        chunks.append(
            Chunk(chunk_no=len(chunks), byte_start=group[0].byte_start, byte_end=group[-1].byte_end, text=text)
        )
    return chunks
