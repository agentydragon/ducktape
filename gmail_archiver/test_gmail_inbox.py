"""Tests for GmailInbox behavior after partial batch fetches."""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from rich.console import Console

from gmail_archiver.gmail_client import GmailClient
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.models import Email


def _email(message_id: str) -> Email:
    return Email(
        id=message_id,
        thread_id=f"thread-{message_id}",
        label_ids=["INBOX"],
        internal_date=datetime(2026, 10, 28, tzinfo=UTC),
        snippet="test message",
        raw_bytes=b"Subject: test\n\nbody",
    )


class _Client:
    def __init__(self) -> None:
        self.batch_requests: list[list[str]] = []
        self.batch_responses: list[tuple[list[Email], list[tuple[str, str]]]] = [
            ([_email("a")], [("b", "HTTP 429")]),
            ([_email("b")], []),
        ]

    def list_messages_by_query(self, query: str) -> list[str]:
        assert query == "in:inbox"
        return ["a", "b"]

    def get_messages_batch(
        self, message_ids: list[str], *, batch_size: int
    ) -> tuple[list[Email], list[tuple[str, str]]]:
        assert batch_size == 2
        self.batch_requests.append(message_ids)
        return self.batch_responses.pop(0)


def test_partial_fetch_raises_and_retains_success_for_retry(tmp_path: Path) -> None:
    client = _Client()
    inbox = GmailInbox(cast(GmailClient, client), Console(), tmp_path, show_progress=False)

    with pytest.raises(RuntimeError, match=r"Failed to fetch Gmail messages.*b"):
        inbox.fetch_messages("in:inbox", batch_size=2)

    messages = inbox.fetch_messages("in:inbox", batch_size=2)

    assert [message.id for message in messages] == ["a", "b"]
    assert client.batch_requests == [["a", "b"], ["b"]]


if __name__ == "__main__":
    pytest_bazel.main()
