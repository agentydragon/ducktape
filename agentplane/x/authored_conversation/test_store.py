import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.x.authored_conversation.settings import Settings
from agentplane.x.authored_conversation.store import Author, Store, open_store


def test_durable_order_and_pagination(settings: Settings, store: Store) -> None:
    first = store.append(Author.HUMAN, "Question")
    second = store.append(Author.AGENT, "Answer")
    third = store.append(Author.HUMAN, "Follow-up")
    with open_store(settings.state_directory / "thread.sqlite", max_message_chars=8000) as reopened:
        page = reopened.read(after_message_id=None, limit=2)
        assert page.messages == [first, second]
        assert page.next_after_message_id == second.id
        last = reopened.read(after_message_id=page.next_after_message_id, limit=2)
        assert last.messages == [third]
        assert last.next_after_message_id is None
        assert reopened.messages(after_position=first.position, author=Author.HUMAN) == [third]
    with pytest.raises(ValueError, match="unknown message cursor"):
        store.read(after_message_id=uuid4(), limit=2)


@pytest.mark.parametrize("body", [" ", "x" * 8001])
def test_invalid_message_does_not_commit(store: Store, body: str) -> None:
    with pytest.raises(ValueError, match="message must contain"):
        store.append(Author.HUMAN, body)
    assert store.messages() == []


def test_edits_keep_order_and_all_revisions_after_reopening(settings: Settings, store: Store) -> None:
    first = store.append(Author.HUMAN, "Original")
    second = store.append(Author.AGENT, "Answer")
    edited = store.edit(first.id, author=Author.HUMAN, expected_revision=1, body="Corrected")
    assert (edited.id, edited.position, edited.author) == (first.id, first.position, first.author)
    assert edited.revision == 2
    assert edited.change_sequence > second.change_sequence
    with open_store(settings.state_directory / "thread.sqlite", max_message_chars=8000) as reopened:
        assert reopened.messages() == [edited, second]
        assert reopened.revisions(first.id) == [first, edited]
        assert reopened.get(first.id, revision=1) == first
        assert reopened.changes(after_sequence=second.change_sequence, author=Author.HUMAN) == [edited]
        assert reopened.read(after_message_id=first.id, limit=1).messages == [second]


def test_forbidden_stale_and_invalid_edits_do_not_create_revisions(store: Store) -> None:
    message = store.append(Author.AGENT, "Original")
    with pytest.raises(ValueError, match="own messages"):
        store.edit(message.id, author=Author.HUMAN, expected_revision=1, body="Takeover")
    with pytest.raises(ValueError, match="revision conflict"):
        store.edit(message.id, author=Author.AGENT, expected_revision=0, body="Stale")
    with pytest.raises(ValueError, match="message must contain"):
        store.edit(message.id, author=Author.AGENT, expected_revision=1, body=" ")
    assert store.revisions(message.id) == [message]


def test_concurrent_editors_cannot_silently_overwrite(store: Store) -> None:
    message = store.append(Author.HUMAN, "Original")
    barrier = Barrier(2)

    def edit(body):
        barrier.wait(timeout=5)
        try:
            return store.edit(message.id, author=Author.HUMAN, expected_revision=1, body=body)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, ["A", "B"]))
    conflicts = [result for result in results if isinstance(result, ValueError)]
    assert len(conflicts) == 1
    assert "revision conflict" in str(conflicts[0])
    assert len(store.revisions(message.id)) == 2


def test_upgrade_preserves_legacy_ids_positions_authors_and_bodies(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    first, second = uuid4(), uuid4()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE message (position INTEGER PRIMARY KEY, id CHAR(32) UNIQUE NOT NULL, "
            "author VARCHAR NOT NULL, body VARCHAR NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            [(4, first.hex, "human", "Keep this\nverbatim"), (9, second.hex, "agent", "Answer")],
        )
    with open_store(path, max_message_chars=8000) as upgraded:
        messages = upgraded.messages()
        assert [(m.position, m.id, m.author, m.body, m.revision) for m in messages] == [
            (4, first, Author.HUMAN, "Keep this\nverbatim", 1),
            (9, second, Author.AGENT, "Answer", 1),
        ]
        upgraded.edit(first, author=Author.HUMAN, expected_revision=1, body="Correction")
    with open_store(path, max_message_chars=8000) as reopened:
        assert len(reopened.revisions(first)) == 2
        assert reopened.get(first, revision=1).body == "Keep this\nverbatim"


def test_newer_database_is_refused_without_changes(tmp_path) -> None:
    path = tmp_path / "future.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer"), open_store(path, max_message_chars=8000):
        pass
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (999,)


if __name__ == "__main__":
    pytest_bazel.main()
