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
class _Line:
    byte_start: int
    byte_end: int
    text: str


def _lines(blob: str) -> Iterator[_Line]:
    """Split into lines that keep their terminators, carrying byte offsets into the blob."""
    offset = 0
    for line in blob.splitlines(keepends=True):
        end = offset + len(line.encode())
        yield _Line(byte_start=offset, byte_end=end, text=line)
        offset = end


def _split_oversized(line: _Line) -> Iterator[_Line]:
    """Hard-split a single line that exceeds the max, on character boundaries."""
    if line.byte_end - line.byte_start <= _MAX_BYTES:
        yield line
        return
    offset = line.byte_start
    buffer = ""
    for char in line.text:
        if len((buffer + char).encode()) > _MAX_BYTES:
            encoded = len(buffer.encode())
            yield _Line(byte_start=offset, byte_end=offset + encoded, text=buffer)
            offset += encoded
            buffer = ""
        buffer += char
    if buffer:
        yield _Line(byte_start=offset, byte_end=offset + len(buffer.encode()), text=buffer)


def _pack(lines: Iterator[_Line]) -> Iterator[list[_Line]]:
    """Greedily pack lines into groups of at most the target size, never splitting a line."""
    group: list[_Line] = []
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
    for group in _pack(part for line in _lines(blob) for part in _split_oversized(line)):
        text = "".join(line.text for line in group)
        if not text.strip():
            continue
        chunks.append(
            Chunk(chunk_no=len(chunks), byte_start=group[0].byte_start, byte_end=group[-1].byte_end, text=text)
        )
    return chunks
