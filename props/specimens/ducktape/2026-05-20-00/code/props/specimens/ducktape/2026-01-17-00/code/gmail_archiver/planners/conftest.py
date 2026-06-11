"""Shared test fixtures."""

from datetime import UTC, datetime
from email.message import EmailMessage as StdEmailMessage

import pytest

from gmail_archiver.models import Email


@pytest.fixture
def make_email():
    def _make_email(
        *,
        id: str = "test-msg-id",
        thread_id: str | None = None,
        sender: str = "test@example.com",
        recipient: str | None = None,
        subject: str = "Test Subject",
        date: str = "Mon, 1 Jan 2024 12:00:00 +0000",
        internal_date: int = 1704110400000,  # 2024-01-01 12:00:00 UTC in ms
        body: str = "",
        snippet: str | None = None,
        label_ids: list[str] | None = None,
    ) -> Email:
        # Build RFC822 message bytes
        msg = StdEmailMessage()
        msg["From"] = sender
        if recipient:
            msg["To"] = recipient
        msg["Subject"] = subject
        msg["Date"] = date
        msg.set_content(body)
        raw_bytes = msg.as_bytes()

        # Convert internal_date from milliseconds to datetime
        internal_dt = datetime.fromtimestamp(internal_date / 1000, tz=UTC)

        return Email(
            id=id,
            thread_id=thread_id,
            label_ids=label_ids or [],
            internal_date=internal_dt,
            snippet=snippet,
            raw_bytes=raw_bytes,
        )

    return _make_email
