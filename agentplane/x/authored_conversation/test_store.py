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


if __name__ == "__main__":
    pytest_bazel.main()
