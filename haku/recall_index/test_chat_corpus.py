"""What a chat chunk holds, including overlap and message provenance at its edges."""

from __future__ import annotations

import datetime
import itertools
import uuid

import pytest_bazel

from haku.recall_index.chat_corpus import IndexedMessage, Speaker, chunk_messages, render_message
from haku.recall_index.chunking import ChunkBudget

_START = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)


def message(content: str, *, speaker: Speaker = Speaker.USER, minute: int = 0) -> IndexedMessage:
    return IndexedMessage(
        message_id=uuid.uuid4(),
        speaker=speaker,
        content=content,
        created_at=_START + datetime.timedelta(minutes=minute),
    )


def test_a_short_exchange_is_one_chunk_holding_every_message() -> None:
    messages = [
        message("how do I file an intake item", minute=0),
        message("put it in inbox.md", speaker=Speaker.ASSISTANT, minute=1),
    ]
    (chunk,) = chunk_messages(messages)
    assert chunk.message_ids == tuple(item.message_id for item in messages)
    assert chunk.first_message_at == messages[0].created_at
    assert chunk.last_message_at == messages[-1].created_at


def test_the_speaker_is_part_of_what_gets_embedded() -> None:
    (chunk,) = chunk_messages([message("intake", speaker=Speaker.ASSISTANT)])
    assert chunk.text == "assistant: intake\n"


def test_windows_overlap_and_name_every_intersected_message() -> None:
    budget = ChunkBudget(target_bytes=260, max_bytes=400, overlap_codepoints=24)
    messages = [message(f"message-{index} " * 16, minute=index) for index in range(6)]
    chunks = chunk_messages(messages, budget)
    assert len(chunks) > 1
    assert all(left.text[-24:] == right.text[:24] for left, right in itertools.pairwise(chunks))
    # The overlap can be part of a message, but each chunk's pointer names all messages whose
    # rendered bytes it actually contains.
    assert any(
        message_id in left.message_ids and message_id in right.message_ids
        for left, right in itertools.pairwise(chunks)
        for message_id in left.message_ids
    )


def test_an_oversized_message_splits_with_overlap_and_keeps_its_pointer() -> None:
    budget = ChunkBudget(target_bytes=100, max_bytes=200, overlap_codepoints=10)
    huge = message("😀" * 300)
    chunks = chunk_messages([huge], budget)
    assert len(chunks) > 1
    assert {chunk.message_ids for chunk in chunks} == {(huge.message_id,)}
    assert all(left.text[-10:] == right.text[:10] for left, right in itertools.pairwise(chunks))
    assert chunks[0].text.startswith("user: ")
    assert chunks[-1].text.endswith("\n")


def test_empty_messages_are_dropped() -> None:
    spoken = message("intake", minute=1)
    chunks = chunk_messages([message("   ", minute=0), spoken])
    assert [chunk.message_ids for chunk in chunks] == [(spoken.message_id,)]


def test_identical_exchanges_share_a_content_address() -> None:
    first = chunk_messages([message("intake", minute=0)])
    second = chunk_messages([message("intake", minute=9)])
    assert first[0].content_sha == second[0].content_sha


def test_chunk_text_is_an_exact_slice_of_the_rendered_transcript() -> None:
    messages = [message("první 😀 message" * 20, minute=index) for index in range(4)]
    transcript = "".join(render_message(item) for item in messages)
    encoded = transcript.encode()
    budget = ChunkBudget(target_bytes=180, max_bytes=300, overlap_codepoints=18)
    for chunk in chunk_messages(messages, budget):
        start = encoded.find(chunk.text.encode())
        assert start >= 0
        assert encoded[start : start + len(chunk.text.encode())].decode() == chunk.text


if __name__ == "__main__":
    pytest_bazel.main()
