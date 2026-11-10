"""Tests for GmailInbox behavior after partial batch fetches."""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from rich.console import Console

from gmail_archiver.gmail_api_models import GmailHeader, GmailMessagePayload, GmailMessageWithHeaders
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


def test_metadata_fetch_uses_metadata_cache_for_all_message_ids(tmp_path: Path) -> None:
    class _MixedCacheClient:
        def __init__(self) -> None:
            self.metadata_requests: list[list[str]] = []

        def list_messages_by_query(self, query: str) -> list[str]:
            return {
                "full-cache-query": ["full-cache-message"],
                "mixed-query": ["full-cache-message", "metadata-cache-message", "uncached-message"],
            }[query]

        def get_messages_batch(
            self, message_ids: list[str], *, batch_size: int
        ) -> tuple[list[Email], list[tuple[str, str]]]:
            assert message_ids == ["full-cache-message"]
            assert batch_size == 50
            return [_email(message_ids[0])], []

        def get_messages_metadata_batch(
            self, message_ids: list[str], *, batch_size: int
        ) -> tuple[list[GmailMessageWithHeaders], list[tuple[str, str]]]:
            assert batch_size == 50
            self.metadata_requests.append(message_ids)
            return [
                GmailMessageWithHeaders(
                    id=message_id,
                    thread_id=f"thread-{message_id}",
                    label_ids=["INBOX", f"label-{message_id}"],
                    internal_date="1793145600000",
                    payload=GmailMessagePayload(
                        headers=[
                            GmailHeader(name="Subject", value=f"Subject for {message_id}"),
                            GmailHeader(name="From", value=f"{message_id}@example.invalid"),
                        ]
                    ),
                )
                for message_id in message_ids
            ], []

    client = _MixedCacheClient()
    inbox = GmailInbox(cast(GmailClient, client), Console(), tmp_path, show_progress=False)
    inbox.fetch_messages("full-cache-query")
    inbox.ensure_metadata_cached(["metadata-cache-message"])

    messages = inbox.fetch_messages_metadata("mixed-query")
    repeated_messages = inbox.fetch_messages_metadata("mixed-query")

    assert [message.id for message in messages] == ["full-cache-message", "metadata-cache-message", "uncached-message"]
    assert [(message.subject, message.sender, message.label_ids) for message in messages] == [
        ("Subject for full-cache-message", "full-cache-message@example.invalid", ["INBOX", "label-full-cache-message"]),
        (
            "Subject for metadata-cache-message",
            "metadata-cache-message@example.invalid",
            ["INBOX", "label-metadata-cache-message"],
        ),
        ("Subject for uncached-message", "uncached-message@example.invalid", ["INBOX", "label-uncached-message"]),
    ]
    assert repeated_messages == messages
    assert client.metadata_requests == [["metadata-cache-message"], ["full-cache-message", "uncached-message"]]


if __name__ == "__main__":
    pytest_bazel.main()
