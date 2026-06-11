"""Text extraction utilities for Anthropic messages."""

from __future__ import annotations

from sysrw.anthropic.types import Message, TextBlock


def extract_text_content(message: Message) -> str:
    """Extract all text content from the message, joining multiple blocks."""
    if isinstance(message.content, str):
        return message.content

    return "\n".join(block.text for block in message.content if isinstance(block, TextBlock))
