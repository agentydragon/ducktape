"""What a chat chunk holds, and how honest that claim stays at the edges."""

from __future__ import annotations

import datetime
import uuid

import pytest_bazel

from haku.console.chat_models import ChatMessageRole
from haku.state_index.chat_corpus import IndexedMessage, chunk_messages

_START = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)


def message(content: str, *, role: ChatMessageRole = ChatMessageRole.USER, minute: int = 0) -> IndexedMessage:
    return IndexedMessage(
        message_id=uuid.uuid4(), role=role, content=content, created_at=_START + datetime.timedelta(minutes=minute)
    )


def test_a_short_exchange_is_one_chunk_holding_every_message() -> None:
    messages = [
        message("how do I file an intake item", minute=0),
        message("put it in inbox.md", role=ChatMessageRole.ASSISTANT, minute=1),
    ]
    (chunk,) = chunk_messages(messages)
    assert chunk.message_ids == tuple(item.message_id for item in messages)
    assert chunk.first_message_at == messages[0].created_at
    assert chunk.last_message_at == messages[-1].created_at


def test_the_speaker_is_part_of_what_gets_embedded() -> None:
    (chunk,) = chunk_messages([message("intake", role=ChatMessageRole.ASSISTANT)])
    assert chunk.text == "assistant: intake\n"


def test_a_long_conversation_splits_at_message_boundaries() -> None:
    messages = [message("filing " * 200, minute=index) for index in range(6)]
    chunks = chunk_messages(messages)
    assert len(chunks) > 1
    # Every message lands in exactly one chunk, and no chunk claims one it does not hold.
    held = [message_id for chunk in chunks for message_id in chunk.message_ids]
    assert held == [item.message_id for item in messages]


def test_a_message_larger_than_a_chunk_splits_but_still_names_only_itself() -> None:
    huge = message("intake " * 2000)
    chunks = chunk_messages([huge])
    assert len(chunks) > 1
    assert {chunk.message_ids for chunk in chunks} == {(huge.message_id,)}
    assert "".join(chunk.text for chunk in chunks) == f"user: {'intake ' * 2000}".strip() + "\n"


def test_empty_messages_are_dropped() -> None:
    spoken = message("intake", minute=1)
    chunks = chunk_messages([message("   ", minute=0), spoken])
    assert [chunk.message_ids for chunk in chunks] == [(spoken.message_id,)]


def test_identical_exchanges_share_a_content_address() -> None:
    first = chunk_messages([message("intake", minute=0)])
    second = chunk_messages([message("intake", minute=9)])
    assert first[0].content_sha == second[0].content_sha


if __name__ == "__main__":
    pytest_bazel.main()
