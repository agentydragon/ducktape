from __future__ import annotations

import json
from collections.abc import Iterable

from fastmcp.client.messages import MessageHandler
from mcp import types

from openai_utils.model import InputTextPart, UserMessage


class ResourceUpdatedCapture(MessageHandler):
    """MessageHandler that captures resource updated notifications."""

    def __init__(self) -> None:
        self.updated: list[str] = []

    async def on_resource_updated(self, message: types.ResourceUpdatedNotification) -> None:
        self.updated.append(str(message.params.uri))


def _iter_text_parts(message: UserMessage) -> Iterable[str]:
    for part in message.content or []:
        if isinstance(part, InputTextPart) and part.text:
            yield part.text


def parse_system_notification_payload(message: str | UserMessage) -> dict:
    """Extract and parse the JSON payload in a tagged system notification message.

    Expects the message to contain:
      <system notification>\n{json}\n</system notification>
    Returns the parsed dict, or raises ValueError on malformed input.
    """
    if isinstance(message, UserMessage):
        parts = list(_iter_text_parts(message))
        if not parts:
            raise ValueError("Message has no text parts to inspect")
        text = "\n".join(parts)
    else:
        text = message

    start_tag = "<system notification>"
    end_tag = "</system notification>"
    start = text.find(start_tag)
    end = text.find(end_tag)
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Not a tagged system notification message")
    payload_str = text[start + len(start_tag) : end].strip()
    result = json.loads(payload_str)
    if not isinstance(result, dict):
        raise TypeError("Payload is not a JSON object")
    return result
