"""Deterministic, code-point-safe chunking of text into embeddable spans.

Chunk boundaries are part of the current retrieval regime key (`CHUNKER_VERSION`), so any
change to the algorithm below must bump that constant — otherwise a reader could select spans
from a different segmentation. All split and overlap boundaries are Unicode code-point
boundaries. ``Span`` still records UTF-8 byte offsets because Git provenance slices blobs by
byte offset.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections.abc import Collection, Iterator
from dataclasses import asdict, dataclass

# Bump on a change to the boundary rules. The size budget is *not* part of this — it travels in
# `chunker_key` instead, so re-tuning it needs no code change and still selects a new regime.
CHUNKER_VERSION = 2


@dataclass(frozen=True, slots=True)
class ChunkBudget:
    """How much text one chunk may hold.

    In bytes rather than tokens, because chunking must not depend on a tokenizer: the model is
    behind an HTTP endpoint, and asking it to count tokens per candidate chunk would put the
    embedder on the chunker's critical path for no retrieval benefit. English prose runs roughly
    four bytes to the token, so a budget states the model's window approximately and deliberately.

    ``target_bytes`` is what packing aims for; ``max_bytes`` is the hard split for a single
    unbroken line — a minified JSON blob, a base64 payload — that the preferred-boundary rule
    cannot divide. ``overlap_codepoints`` is trailing context carried into the next chunk.

    Byte budgets bound the input size sent to an embedding model. Overlap is measured in Unicode
    code points so a configured amount never cuts a UTF-8 sequence in half.
    """

    target_bytes: int
    max_bytes: int
    overlap_codepoints: int = 0

    def __post_init__(self) -> None:
        if self.target_bytes <= 0 or self.max_bytes < self.target_bytes:
            raise ValueError(f"nonsensical chunk budget: {self}")
        if self.overlap_codepoints < 0:
            raise ValueError(f"negative chunk overlap: {self}")
        # A Unicode scalar is at most four bytes in UTF-8. This guarantees a max-sized window
        # always has room to advance beyond the configured overlap, even for four-byte text.
        if self.overlap_codepoints and self.overlap_codepoints * 4 >= self.max_bytes:
            raise ValueError(f"chunk overlap leaves no room for progress: {self}")


# Conservative rather than tuned: sized for a model with a 512-token window, where the one in use
# now has a far larger one. Raising it is a retrieval question — bigger chunks match more broadly
# and cite less precisely — hence a knob and not a constant.
DEFAULT_CHUNK_BUDGET = ChunkBudget(target_bytes=1500, max_bytes=3000, overlap_codepoints=128)


def regime_key(version: int, budget: ChunkBudget) -> str:
    """What identifies chunks produced by one chunker under one budget.

    Both halves matter: the same bytes chunked by different rules, or to a different size, are
    different retrieval units. Serialized from the budget rather than formatted by hand, which is
    the point — a field added to ``ChunkBudget`` lands in the key without anyone remembering to
    put it there. Canonical: sorted keys and no whitespace, so the same regime is the same bytes
    in every process. Text rather than ``jsonb`` because this is a primary-key component compared
    for equality, and a canonical string is the simpler contract.
    """
    return json.dumps({"version": version, **asdict(budget)}, sort_keys=True, separators=(",", ":"))


def git_chunker_key(budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> str:
    return regime_key(CHUNKER_VERSION, budget)


@dataclass(frozen=True, slots=True)
class Span:
    """A run of text and where it sits, in bytes, inside the document it came from."""

    byte_start: int
    byte_end: int
    text: str


def _utf8_offsets(text: str) -> list[int]:
    """The UTF-8 byte offset at every Unicode code-point boundary in ``text``."""
    offsets = [0]
    for character in text:
        offsets.append(offsets[-1] + len(character.encode()))
    return offsets


def _end_within(offsets: list[int], start: int, byte_limit: int) -> int:
    """Furthest code-point boundary from ``start`` that fits ``byte_limit``.

    A one-code-point chunk may exceed the byte limit when one code point itself is larger than
    the limit. It is the only useful response: splitting that code point is not valid UTF-8.
    """
    end = bisect_right(offsets, offsets[start] + byte_limit) - 1
    return max(start + 1, end)


def chunk_windows(text: str, *, budget: ChunkBudget, preferred_ends: Collection[int] = ()) -> Iterator[Span]:
    """Yield overlapping UTF-8 spans, preferring supplied code-point boundary positions.

    ``preferred_ends`` contains character indexes *after* source units such as lines or chat
    messages. The widest preferred boundary within target wins. If none fits target, one within
    max wins; an unbroken oversized unit finally hard-splits at a code-point boundary.
    """
    if not text:
        return
    offsets = _utf8_offsets(text)
    ends = sorted(set(preferred_ends))
    start = 0
    while start < len(text):
        target_end = _end_within(offsets, start, budget.target_bytes)
        max_end = _end_within(offsets, start, budget.max_bytes)
        target_boundary = bisect_right(ends, target_end) - 1
        if target_boundary >= 0 and ends[target_boundary] > start:
            end = ends[target_boundary]
        else:
            max_boundary = bisect_right(ends, max_end) - 1
            end = ends[max_boundary] if max_boundary >= 0 and ends[max_boundary] > start else max_end

        yield Span(byte_start=offsets[start], byte_end=offsets[end], text=text[start:end])
        if end == len(text):
            return
        start = max(start + 1, end - budget.overlap_codepoints)


def split_utf8(
    text: str, max_bytes: int, *, byte_start: int = 0, size: int | None = None, overlap_codepoints: int = 0
) -> Iterator[Span]:
    """Hard-split ``text`` at Unicode code-point boundaries with optional trailing overlap.

    ``size`` avoids a redundant encoding in the common one-span case. Returned offsets remain
    relative to the caller's source bytes.
    """
    if overlap_codepoints < 0:
        raise ValueError(f"negative chunk overlap: {overlap_codepoints}")
    if overlap_codepoints and overlap_codepoints * 4 >= max_bytes:
        raise ValueError(f"chunk overlap leaves no room for progress: {overlap_codepoints}")
    encoded_size = len(text.encode()) if size is None else size
    if encoded_size <= max_bytes:
        yield Span(byte_start=byte_start, byte_end=byte_start + encoded_size, text=text)
        return
    budget = ChunkBudget(target_bytes=max_bytes, max_bytes=max_bytes, overlap_codepoints=overlap_codepoints)
    for span in chunk_windows(text, budget=budget):
        yield Span(byte_start=byte_start + span.byte_start, byte_end=byte_start + span.byte_end, text=span.text)


def chunk_text(blob: str, budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> list[Span]:
    """Chunk a decoded blob into overlapping embeddable spans.

    Line ends are preferred cuts, but an oversized line hard-splits at a Unicode code-point
    boundary. Blank-only spans are dropped: they embed to noise and would only ever be retrieved
    by accident. Offsets remain byte offsets into the blob as stored in Git, so a caller holding
    the blob can slice the exact embedded text back out.
    """
    line_ends: list[int] = []
    position = 0
    for line in blob.splitlines(keepends=True):
        position += len(line)
        line_ends.append(position)
    return [span for span in chunk_windows(blob, budget=budget, preferred_ends=line_ends) if span.text.strip()]
