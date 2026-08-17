"""The chat corpus: the console's own record of what was said, made searchable.

The corpus is the console's `session_messages` — every session it has served, Matrix and SPA
alike. This module is the shape of a chunk; `chat_source.py` is where the rows come from, so the
store can depend on the former without dragging the console's whole schema behind it.

**Message boundaries are preferred, not required.** Packing selects a message boundary whenever
one fits the configured budget, then carries Unicode-code-point overlap into the following
window. The overlap and an oversized message can therefore cut a message, but each window names
every message it intersects (`schema.ChatChunkMessage`), so a hit still hands back exact pointers
to drill into.

Frames (`session_frames`) are deliberately not indexed here; see this package's README.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from haku.console.chat_models import ChatMessageRole
from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget, chunk_windows, regime_key
from haku.recall_index.content import content_sha

# Bump on a change to the rendering or packing below. Scoped to this corpus: the git chunker's
# version moves independently, and the size budget travels in the key rather than here.
CHAT_CHUNKER_VERSION = 2


def chat_chunker_key(budget: ChunkBudget = DEFAULT_CHUNK_BUDGET) -> str:
    """What identifies chunks produced by this chunker, for the cache key."""
    return regime_key(CHAT_CHUNKER_VERSION, budget)


@dataclass(frozen=True, slots=True)
class IndexedMessage:
    message_id: UUID
    role: ChatMessageRole
    content: str
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class MessageChunk:
    """One embeddable window of a session, and the messages it holds."""

    window_no: int
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
        return content_sha(self.text)


@dataclass(frozen=True, slots=True)
class _Rendered:
    message: IndexedMessage
    text: str
    byte_start: int
    byte_end: int


def render_message(message: IndexedMessage) -> str:
    """The text the embedder sees for one message.

    The speaker is part of it: "what did I ask about X" and "what did it answer about X" are
    different queries, and a bare content string cannot tell them apart.
    """
    return f"{message.role}: {message.content.strip()}\n"


def chunk_messages(
    messages: Sequence[IndexedMessage], budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
) -> list[MessageChunk]:
    """Chunk a session's messages into embeddable windows, in conversation order.

    Empty messages are dropped: they carry nothing to match on, and an assistant row can be
    empty when the turn's text arrived only on the result frame.
    """
    rendered: list[_Rendered] = []
    text_parts: list[str] = []
    offset = 0
    character_offset = 0
    preferred_ends: list[int] = []
    for message in messages:
        if not message.content.strip():
            continue
        text = render_message(message)
        encoded = len(text.encode())
        rendered.append(_Rendered(message=message, text=text, byte_start=offset, byte_end=offset + encoded))
        text_parts.append(text)
        offset += encoded
        character_offset += len(text)
        preferred_ends.append(character_offset)

    transcript = "".join(text_parts)
    chunks: list[MessageChunk] = []
    for span in chunk_windows(transcript, budget=budget, preferred_ends=preferred_ends):
        covered = [item for item in rendered if item.byte_start < span.byte_end and span.byte_start < item.byte_end]
        # ``span`` comes from the rendered transcript, so it must overlap at least one rendered
        # message. Keeping this explicit makes a future renderer change fail loudly rather than
        # returning a window with invented timestamps or no drilldown pointer.
        if not covered:
            raise AssertionError("chat chunk did not overlap a source message")
        chunks.append(
            MessageChunk(
                window_no=len(chunks),
                message_ids=tuple(item.message.message_id for item in covered),
                text=span.text,
                first_message_at=covered[0].message.created_at,
                last_message_at=covered[-1].message.created_at,
            )
        )
    return chunks
