"""The chat corpus: the console's own record of what was said, made searchable.

The corpus is the console's `claude_chat_messages` — every Claude chat session it has served,
Matrix and SPA alike. This module is the shape of a chunk; `chat_source.py` is where the rows
come from, and keeping the two apart is what lets the store depend on the former without
dragging the console's whole schema behind it.

**A chunk holds whole messages.** Packing stops at message boundaries rather than at lines, so
every chunk can name exactly which messages it covers (`schema.ChatChunkMessage`) and a hit
hands back pointers a caller can drill into with the console's conversation tools. The one
exception is a message longer than a whole chunk, which is split — and then each part still
holds exactly that one message, so the mapping never becomes approximate.

Frames (`claude_chat_frames`) are deliberately not indexed here; see this package's README.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import UUID

from haku.console.chat_models import ChatMessageRole
from haku.state_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget, split_utf8

# Bump on a change to the rendering or packing below. Scoped to this corpus: the git chunker's
# version moves independently, and the size budget travels in the key rather than here.
CHAT_CHUNKER_VERSION = 1


def chat_chunker_key(budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> str:
    """What identifies chunks produced by this chunker, for the cache key."""
    return f"v{CHAT_CHUNKER_VERSION}/{budget.key}"


@dataclass(frozen=True, slots=True)
class IndexedMessage:
    message_id: UUID
    role: ChatMessageRole
    content: str
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class MessageChunk:
    """One embeddable window of a session, and the messages it holds."""

    chunk_no: int
    message_ids: tuple[UUID, ...]
    text: str
    first_message_at: datetime.datetime
    last_message_at: datetime.datetime

    @property
    def content_sha(self) -> str:
        """This corpus's content address: the hash of the text that gets embedded.

        Two sessions that held the same exchange verbatim therefore share one cached vector,
        the way two paths holding the same blob do on the git side.
        """
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _Rendered:
    message: IndexedMessage
    text: str
    size: int


def render_message(message: IndexedMessage) -> str:
    """The text the embedder sees for one message.

    The speaker is part of it: "what did I ask about X" and "what did it answer about X" are
    different queries, and a bare content string cannot tell them apart.
    """
    return f"{message.role}: {message.content.strip()}\n"


def _pack(rendered: Sequence[_Rendered], budget: ChunkBudget) -> Iterator[list[_Rendered]]:
    """Group whole messages up to the target size; an oversized message is its own group."""
    group: list[_Rendered] = []
    used = 0
    for item in rendered:
        if group and (used + item.size > budget.target_bytes or item.size > budget.max_bytes):
            yield group
            group, used = [], 0
        group.append(item)
        used += item.size
        if item.size > budget.max_bytes:
            yield group
            group, used = [], 0
    if group:
        yield group


def chunk_messages(
    messages: Sequence[IndexedMessage], budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
) -> list[MessageChunk]:
    """Chunk a session's messages into embeddable windows, in conversation order.

    Empty messages are dropped: they carry nothing to match on, and an assistant row can be
    empty when the turn's text arrived only on the result frame.
    """
    rendered = [
        _Rendered(message=message, text=rendering, size=len(rendering.encode()))
        for message, rendering in ((message, render_message(message)) for message in messages)
        if message.content.strip()
    ]
    chunks: list[MessageChunk] = []
    for group in _pack(rendered, budget):
        message_ids = tuple(item.message.message_id for item in group)
        # Only a lone oversized message yields more than one span, so every part of a split
        # still holds exactly the message it was split from.
        for span in split_utf8("".join(item.text for item in group), budget.max_bytes):
            chunks.append(
                MessageChunk(
                    chunk_no=len(chunks),
                    message_ids=message_ids,
                    text=span.text,
                    first_message_at=group[0].message.created_at,
                    last_message_at=group[-1].message.created_at,
                )
            )
    return chunks
