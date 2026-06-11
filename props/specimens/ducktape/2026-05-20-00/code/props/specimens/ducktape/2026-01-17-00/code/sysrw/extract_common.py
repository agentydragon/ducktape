from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

from .constants import TOOLS_HEADER
from .openai_typing import (
    MessageRole,
    ResponseContentPart,
    ResponseOutputMessage,
    iter_resolved_text,
    parse_response_messages,
    response_message_content_as_text,
    response_message_role,
)


def sys_has_tools_header(system: str | list[ResponseContentPart] | None) -> bool:
    """Return True if system text (string or list-of-parts) contains tools header."""
    if isinstance(system, str):
        return TOOLS_HEADER in system
    if system is None:
        return False
    # system is list[ResponseContentPart] at this point
    return any(TOOLS_HEADER in text for text in iter_resolved_text(system))


def find_last_user_text_from_msg(msg: ResponseOutputMessage) -> str | None:
    """Extract plain text from a single user message object (CCR-style)."""
    if response_message_role(msg) != MessageRole.USER:
        return None
    return response_message_content_as_text(msg) or None


def find_last_user_text_from_messages(messages: list[ResponseOutputMessage] | Any) -> str | None:
    """Extract last user text from a list of messages (OpenAI chat/Responses).

    Returns None if messages cannot be parsed or last message is not from user.
    """
    parsed = parse_response_messages(messages)
    if not parsed:
        return None
    last = parsed[-1]
    if response_message_role(last) == MessageRole.USER:
        return response_message_content_as_text(last) or None
    return None


def iter_wire_lines(path: Path) -> Iterator[str]:
    """Yield lines from a possibly gzipped file; ignore encoding errors."""
    if not path.exists():
        return

    def _gzip_open(p: Path) -> TextIO:
        return gzip.open(p, "rt", encoding="utf-8", errors="ignore")

    def _plain_open(p: Path) -> TextIO:
        return p.open(encoding="utf-8", errors="ignore")

    opener: Callable[[Path], TextIO] = _gzip_open if str(path).endswith(".gz") else _plain_open
    with opener(path) as f:
        yield from f


def maybe_extract_payload(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Return embedded provider payload dict when present (Crush wire logs)."""
    p = obj.get("payload")
    return p if isinstance(p, dict) else None


# ---------------------------------------------------------------------------
# Shared helpers for dataset extraction scripts
# ---------------------------------------------------------------------------


def write_jsonl_batches(results: list[list[dict]], output_path: Path, *, event: str) -> None:
    """Write a 2D list of JSON-serializable dicts to a JSONL file and emit a summary.

    - results: list of batches (each batch is a list of dict records)
    - output_path: destination file path
    - event: event name to include in the summary line printed to stdout
    """
    count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as out:
        for batch in results:
            for dp in batch:
                out.write(json.dumps(dp, ensure_ascii=False) + "\n")
                count += 1
    print(json.dumps({"event": event, "count": count, "path": str(output_path)}))
