"""Claude transcript titles and the prompt fallback used by the archive."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ClaudeTitleSource(StrEnum):
    PROMPT = "prompt"
    SUMMARY = "summary"
    CUSTOM = "custom"


@dataclass(frozen=True)
class ClaudeTitle:
    text: str
    source: ClaudeTitleSource


def select_claude_title(current: ClaudeTitle | None, record: Mapping[str, Any]) -> ClaudeTitle | None:
    if record.get("type") == "custom-title":
        title = record.get("customTitle")
        if isinstance(title, str) and title.strip():
            return ClaudeTitle(text=title.strip()[:500], source=ClaudeTitleSource.CUSTOM)
    if current is not None and current.source == ClaudeTitleSource.CUSTOM:
        return current
    if record.get("type") == "summary":
        title = record.get("summary")
        if isinstance(title, str) and title.strip():
            return ClaudeTitle(text=title.strip()[:500], source=ClaudeTitleSource.SUMMARY)
    if current is not None or record.get("type") != "user" or record.get("isMeta") is True:
        return current
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        candidates = [content]
    elif isinstance(content, list):
        candidates = [
            part["text"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
    else:
        return None
    for candidate in candidates:
        text = candidate.strip()
        if text.startswith("<fork-boilerplate>"):
            _, _, text = text.partition("</fork-boilerplate>")
            text = text.strip().removeprefix("Your directive:").strip()
        if text and not text.startswith(("<command-name>", "<local-command-stdout>", "<local-command-stderr>")):
            return ClaudeTitle(text=text[:500], source=ClaudeTitleSource.PROMPT)
    return None
