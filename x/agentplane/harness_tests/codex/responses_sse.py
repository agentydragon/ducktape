"""OpenAI Responses API streams, shaped like the packets a real route emits."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any

from x.agentplane.harness_tests.scripted_upstream import Packet, Stream


@dataclass(frozen=True)
class Reasoning:
    summary: str
    # Opaque to the client; the harness must echo it back unchanged on the next request.
    encrypted_content: str


@dataclass(frozen=True)
class Message:
    text: str


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


Item = Reasoning | Message | FunctionCall

# Codex keys items and responses by id, so every scripted one needs a fresh id.
_ids = itertools.count(1)

_USAGE = {
    "input_tokens": 10,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 5,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 15,
}


class _Emitter:
    def __init__(self) -> None:
        self.packets: list[Packet] = []

    def emit(self, data: dict[str, Any]) -> None:
        data = {**data, "sequence_number": len(self.packets)}
        self.packets.append(Packet(data["type"], f"data: {json.dumps(data)}\n\n".encode()))


def _completed_item(item: Item) -> dict[str, Any]:
    match item:
        case Reasoning(summary, encrypted_content):
            return {
                "id": f"rs_test_{next(_ids)}",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": summary}],
                "content": [],
                "encrypted_content": encrypted_content,
            }
        case Message(text):
            return {
                "id": f"msg_test_{next(_ids)}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "annotations": [], "text": text}],
            }
        case FunctionCall(call_id, name, arguments):
            return {
                "id": f"fc_test_{next(_ids)}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }


def _item_packets(emitter: _Emitter, index: int, item: Item) -> dict[str, Any]:
    done = _completed_item(item)
    item_id = done["id"]
    match item:
        case Reasoning(summary, _):
            emitter.emit({"type": "response.output_item.added", "output_index": index, "item": {**done, "summary": []}})
            part: dict[str, Any] = {"type": "summary_text", "text": ""}
            emitter.emit(
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": item_id,
                    "output_index": index,
                    "summary_index": 0,
                    "part": part,
                }
            )
            emitter.emit(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": item_id,
                    "output_index": index,
                    "summary_index": 0,
                    "delta": summary,
                }
            )
            emitter.emit(
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": item_id,
                    "output_index": index,
                    "summary_index": 0,
                    "text": summary,
                }
            )
            emitter.emit(
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": item_id,
                    "output_index": index,
                    "summary_index": 0,
                    "part": {**part, "text": summary},
                }
            )
        case Message(text):
            emitter.emit(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**done, "status": "in_progress", "content": []},
                }
            )
            part = {"type": "output_text", "annotations": [], "text": ""}
            emitter.emit(
                {
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": part,
                }
            )
            emitter.emit(
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "delta": text,
                }
            )
            emitter.emit(
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "text": text,
                }
            )
            emitter.emit(
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": {**part, "text": text},
                }
            )
        case FunctionCall(_, _, arguments):
            emitter.emit(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**done, "status": "in_progress", "arguments": ""},
                }
            )
            emitter.emit(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item_id,
                    "output_index": index,
                    "delta": json.dumps(arguments),
                }
            )
            emitter.emit(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item_id,
                    "output_index": index,
                    "arguments": json.dumps(arguments),
                }
            )
    emitter.emit({"type": "response.output_item.done", "output_index": index, "item": done})
    return done


def response_stream(items: list[Item], *, model: str) -> Stream:
    envelope = {"id": f"resp_test_{next(_ids)}", "object": "response", "created_at": 0, "model": model, "output": []}
    emitter = _Emitter()
    emitter.emit({"type": "response.created", "response": {**envelope, "status": "in_progress"}})
    emitter.emit({"type": "response.in_progress", "response": {**envelope, "status": "in_progress"}})
    output = [_item_packets(emitter, index, item) for index, item in enumerate(items)]
    emitter.emit(
        {
            "type": "response.completed",
            "response": {**envelope, "status": "completed", "output": output, "usage": _USAGE},
        }
    )
    return Stream((*emitter.packets, Packet("done", b"data: [DONE]\n\n")))
