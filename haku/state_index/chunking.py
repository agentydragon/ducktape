"""Deterministic chunking of a text blob into embeddable spans.

Chunk boundaries are part of the embedding cache key (`CHUNKER_VERSION`), so any change to
the algorithm below must bump that constant — otherwise cached vectors from the previous
regime are silently reused for text they were never computed over.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

# Bump on a change to the boundary rules. The size budget is *not* part of this — it travels in
# `chunker_key` instead, so re-tuning it needs no code change and still invalidates the cache.
CHUNKER_VERSION = 1


@dataclass(frozen=True, slots=True)
class ChunkBudget:
    """How much text one chunk may hold.

    In bytes rather than tokens, because chunking must not depend on a tokenizer: the model is
    behind an HTTP endpoint, and asking it to count tokens per candidate chunk would put the
    embedder on the chunker's critical path for no retrieval benefit. English prose runs roughly
    four bytes to the token, so a budget states the model's window approximately and deliberately.

    `target` is what packing aims for; `max` is the hard split for a single unbroken line — a
    minified JSON blob, a base64 payload — that the line rule cannot divide.
    """

    target_bytes: int
    max_bytes: int

    def __post_init__(self) -> None:
        if self.target_bytes <= 0 or self.max_bytes < self.target_bytes:
            raise ValueError(f"nonsensical chunk budget: {self}")

    @property
    def key(self) -> str:
        return f"t{self.target_bytes}m{self.max_bytes}"


# Conservative rather than tuned: it was chosen for a model with a 512-token window, and the one
# in use now has a far larger one. Raising it is a retrieval question — bigger chunks match more
# broadly and cite less precisely — which is why it is a knob and not a constant.
DEFAULT_CHUNK_BUDGET = ChunkBudget(target_bytes=1500, max_bytes=3000)


def git_chunker_key(budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> str:
    """What identifies chunks produced by this chunker, for the cache key.

    Both halves matter: the same bytes chunked by different rules, or to a different size, are
    different text, and vectors computed over one must never answer for the other.
    """
    return f"v{CHUNKER_VERSION}/{budget.key}"


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


def _pack(lines: Iterator[Span], target_bytes: int) -> Iterator[list[Span]]:
    """Greedily pack lines into groups of at most the target size, never splitting a line."""
    group: list[Span] = []
    for line in lines:
        span = line.byte_end - line.byte_start
        if group and (group[-1].byte_end - group[0].byte_start) + span > target_bytes:
            yield group
            group = []
        group.append(line)
    if group:
        yield group


def chunk_text(blob: str, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> list[Chunk]:
    """Chunk a decoded blob into embeddable spans.

    Blank-only spans are dropped: they embed to noise and would only ever be retrieved by
    accident. Offsets are byte offsets into the blob as stored in git, so a caller holding
    the blob can slice the exact span back out.
    """
    chunks: list[Chunk] = []
    parts = (
        part
        for line in _lines(blob)
        for part in split_utf8(
            line.text, budget.max_bytes, byte_start=line.byte_start, size=line.byte_end - line.byte_start
        )
    )
    for group in _pack(parts, budget.target_bytes):
        text = "".join(line.text for line in group)
        if not text.strip():
            continue
        chunks.append(
            Chunk(chunk_no=len(chunks), byte_start=group[0].byte_start, byte_end=group[-1].byte_end, text=text)
        )
    return chunks
