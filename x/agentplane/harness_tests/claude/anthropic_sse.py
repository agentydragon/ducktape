"""Anthropic Messages API responses, shaped like the packets a real route emits."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any, Literal

from x.agentplane.harness_tests.scripted_upstream import Body, Packet, Stream


@dataclass(frozen=True)
class Thinking:
    thinking: str
    # Opaque to the client; the harness must echo it back unchanged with the thinking text.
    signature: str


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


Block = Thinking | Text | ToolUse
StopReason = Literal["end_turn", "tool_use"]

_USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}
# Claude Code keys transcript messages by id, so every scripted message needs a fresh one.
_message_ids = (f"msg_test_{n}" for n in itertools.count(1))


def _packet(event: str, data: dict[str, Any], *, kind: str | None = None) -> Packet:
    return Packet(kind or event, f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())


def _delta(index: int, delta: dict[str, Any]) -> Packet:
    return _packet(
        "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}, kind=delta["type"]
    )


def _block_packets(index: int, block: Block) -> list[Packet]:
    match block:
        case Thinking(thinking, signature):
            start: dict[str, Any] = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": thinking},
                {"type": "signature_delta", "signature": signature},
            ]
        case Text(text):
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": text}]
        case ToolUse(id, name, input):
            start = {"type": "tool_use", "id": id, "name": name, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(input)}]
    return [
        _packet("content_block_start", {"type": "content_block_start", "index": index, "content_block": start}),
        *(_delta(index, delta) for delta in deltas),
        _packet("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


def _stop_reason(blocks: list[Block]) -> StopReason:
    return "tool_use" if any(isinstance(block, ToolUse) for block in blocks) else "end_turn"


def message_stream(blocks: list[Block], *, model: str) -> Stream:
    message: dict[str, Any] = {
        "id": next(_message_ids),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": _USAGE,
    }
    packets = [_packet("message_start", {"type": "message_start", "message": message})]
    for index, block in enumerate(blocks):
        packets.extend(_block_packets(index, block))
    packets.append(
        _packet(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": _stop_reason(blocks), "stop_sequence": None},
                "usage": {"output_tokens": 12},
            },
        )
    )
    packets.append(_packet("message_stop", {"type": "message_stop"}))
    return Stream(tuple(packets))


def message_body(blocks: list[Block], *, model: str) -> Body:
    """The non-streaming form Claude Code falls back to after a lost stream."""
    content: list[dict[str, Any]] = []
    for block in blocks:
        match block:
            case Thinking(thinking, signature):
                content.append({"type": "thinking", "thinking": thinking, "signature": signature})
            case Text(text):
                content.append({"type": "text", "text": text})
            case ToolUse(id, name, input):
                content.append({"type": "tool_use", "id": id, "name": name, "input": input})
    message: dict[str, Any] = {
        "id": next(_message_ids),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _stop_reason(blocks),
        "stop_sequence": None,
        "usage": {**_USAGE, "output_tokens": 12},
    }
    return Body(json.dumps(message).encode())
