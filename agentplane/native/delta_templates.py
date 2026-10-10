"""Exact shapes of the streamed delta frames whose removal loses only how a response was chunked.

A frame matches only when its parsed JSON has exactly a template's keys at every level and every
value has the template's type. Each field is a literal, an identity the caller compares with the
item's other frames, the chunk's content, a group key, or per-chunk data the template drops (the
frame id, a timestamp, a token estimate). Anything else, including a key a newer harness adds, is
not a match. The shapes were taken from staging frames on 2026-10-10 (Claude Code 2.1.x, Codex
app-server); a template is added, never edited, so a stored template name keeps its meaning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeIs


class DeltaTemplate(StrEnum):
    CLAUDE_TEXT = "claude.stream_event.text_delta@1"
    CLAUDE_THINKING = "claude.stream_event.thinking_delta@1"
    CLAUDE_INPUT_JSON = "claude.stream_event.input_json_delta@1"
    CODEX_AGENT_MESSAGE = "codex.item/agentMessage/delta@1"
    CODEX_REASONING_SUMMARY = "codex.item/reasoning/summaryTextDelta@1"
    CODEX_COMMAND_OUTPUT = "codex.item/commandExecution/outputDelta@1"


@dataclass(frozen=True)
class ClaudeDelta:
    template: DeltaTemplate
    session_id: str
    parent_tool_use_id: str | None
    index: int
    content: str


@dataclass(frozen=True)
class CodexDelta:
    template: DeltaTemplate
    thread_id: str
    turn_id: str
    item_id: str
    content: str
    # Codex reasoning summary part; None for templates without one.
    summary_index: int | None


class DuplicateKeyError(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise DuplicateKeyError("duplicate JSON object key")
    return result


def parse_strict(line: str) -> dict[str, object] | None:
    """The frame's JSON object, or None for anything a lossy parse could misrepresent."""
    try:
        value = json.loads(line, object_pairs_hook=_unique_keys)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _is_int(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool)


_CLAUDE_DELTAS: dict[str, tuple[DeltaTemplate, str, tuple[frozenset[str], ...]]] = {
    "text_delta": (DeltaTemplate.CLAUDE_TEXT, "text", (frozenset({"type", "text"}),)),
    "thinking_delta": (
        DeltaTemplate.CLAUDE_THINKING,
        "thinking",
        (frozenset({"type", "thinking"}), frozenset({"type", "thinking", "estimated_tokens"})),
    ),
    "input_json_delta": (DeltaTemplate.CLAUDE_INPUT_JSON, "partial_json", (frozenset({"type", "partial_json"}),)),
}

_CODEX_DELTAS: dict[str, tuple[DeltaTemplate, frozenset[str]]] = {
    "item/agentMessage/delta": (
        DeltaTemplate.CODEX_AGENT_MESSAGE,
        frozenset({"threadId", "turnId", "itemId", "delta"}),
    ),
    "item/reasoning/summaryTextDelta": (
        DeltaTemplate.CODEX_REASONING_SUMMARY,
        frozenset({"threadId", "turnId", "itemId", "delta", "summaryIndex"}),
    ),
    "item/commandExecution/outputDelta": (
        DeltaTemplate.CODEX_COMMAND_OUTPUT,
        frozenset({"threadId", "turnId", "itemId", "delta"}),
    ),
}


def match_delta(frame: dict[str, object]) -> ClaudeDelta | CodexDelta | None:
    if frame.keys() == {"type", "event", "session_id", "parent_tool_use_id", "uuid"}:
        return _match_claude(frame)
    if frame.keys() == {"method", "params", "emittedAtMs"}:
        return _match_codex(frame)
    return None


def _match_claude(frame: dict[str, object]) -> ClaudeDelta | None:
    event, session_id, parent = frame["event"], frame["session_id"], frame["parent_tool_use_id"]
    if (
        frame["type"] != "stream_event"
        or not isinstance(frame["uuid"], str)
        or not isinstance(session_id, str)
        or not (parent is None or isinstance(parent, str))
        or not isinstance(event, dict)
        or event.keys() != {"type", "index", "delta"}
        or event["type"] != "content_block_delta"
    ):
        return None
    index, delta = event["index"], event["delta"]
    if not _is_int(index) or not isinstance(delta, dict):
        return None
    kind = delta.get("type")
    known = _CLAUDE_DELTAS.get(kind) if isinstance(kind, str) else None
    if known is None:
        return None
    template, content_key, key_sets = known
    content = delta.get(content_key)
    if (
        delta.keys() not in key_sets
        or not isinstance(content, str)
        or ("estimated_tokens" in delta and not _is_int(delta["estimated_tokens"]))
    ):
        return None
    return ClaudeDelta(template, session_id, parent, index, content)


def _match_codex(frame: dict[str, object]) -> CodexDelta | None:
    method, params = frame["method"], frame["params"]
    if not isinstance(method, str) or not _is_int(frame["emittedAtMs"]) or not isinstance(params, dict):
        return None
    known = _CODEX_DELTAS.get(method)
    if known is None or params.keys() != known[1]:
        return None
    thread_id, turn_id, item_id, delta = params["threadId"], params["turnId"], params["itemId"], params["delta"]
    summary_index = params.get("summaryIndex")
    if not (
        isinstance(thread_id, str)
        and isinstance(turn_id, str)
        and isinstance(item_id, str)
        and isinstance(delta, str)
        and (summary_index is None or (_is_int(summary_index) and summary_index >= 0))
    ):
        return None
    return CodexDelta(known[0], thread_id, turn_id, item_id, delta, summary_index)
