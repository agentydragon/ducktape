"""Models for Gmail data structures."""

from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from email.utils import parsedate_to_datetime
from functools import cached_property
from typing import Self

from gmail_archiver.gmail_api_models import GmailMessageMinimal
from gmail_archiver.html_utils import html_to_text


def parse_email_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None


@dataclass
class Email:
    """Email with lazy parsing from raw bytes.

    Stores raw RFC 2822 bytes as the single source of truth.
    All parsed fields are derived on demand via cached_property.
    """

    # Gmail metadata
    id: str
    thread_id: str | None
    label_ids: list[str]
    internal_date: datetime
    snippet: str | None

    # Raw content - single source of truth
    raw_bytes: bytes

    @cached_property
    def _parsed(self) -> EmailMessage:
        return BytesParser(policy=default).parsebytes(self.raw_bytes)

    @cached_property
    def sender(self) -> str:
        return self._parsed.get("From", "")

    @cached_property
    def recipient(self) -> str | None:
        return self._parsed.get("To")

    @cached_property
    def subject(self) -> str:
        return self._parsed.get("Subject", "")

    @cached_property
    def date(self) -> datetime | None:
        return parse_email_date(self._parsed.get("Date"))

    @cached_property
    def text_plain(self) -> str | None:
        for part in self._parsed.walk():
            if part.get_content_type() == "text/plain":
                content = part.get_content()
                return str(content) if content is not None else None
        return None

    @cached_property
    def text_html(self) -> str | None:
        for part in self._parsed.walk():
            if part.get_content_type() == "text/html":
                content = part.get_content()
                return str(content) if content is not None else None
        return None

    def get_text(self) -> str:
        """Best available text: plain if exists, else converted HTML."""
        if self.text_plain:
            return self.text_plain
        if self.text_html:
            return html_to_text(self.text_html)
        return ""

    @classmethod
    def from_gmail_response(cls, raw_bytes: bytes, metadata: GmailMessageMinimal) -> Self:
        """Create Email from raw bytes and Gmail API metadata."""
        return cls(
            id=metadata.id,
            thread_id=metadata.thread_id,
            label_ids=metadata.label_ids,
            internal_date=datetime.fromtimestamp(int(metadata.internal_date) / 1000, tz=UTC),
            snippet=metadata.snippet,
            raw_bytes=raw_bytes,
        )
