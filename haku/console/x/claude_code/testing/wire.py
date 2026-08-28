"""The CLI's frames, as a test writes them.

One builder per shape the readers in <../frames.py> pick a value out of, so a test says what it is
building — an assistant frame that made one tool call, a tool result that failed — and the JSON
stays here beside the code that reads it.

Fidelity is to <../testdata/diverse_session.jsonl> and to `protocol.md`: envelope keys
every real frame carries are emitted
whether or not anything reads them, because what a reader ignores is as much a fact about it
as what it reads. What varies between real frames is a parameter; what is constant on the wire is
not.

One absence is deliberate rather than laziness, and it is a state the wire really produces:
`message_id` defaults to absent, since 1,417 production assistant rows carry none and a frame with
no id cannot be grouped, so it is its own message.

Frames deliberately outside a builder: a class no release has seen (the point of a test is that
the shape is unknown), and a frame missing the very field its test is about. Both are clearer
written out.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# One session's identity across a whole fixture, since no builder here spans two.
SESSION_ID = "a2d5"

# Present on every production `assistant` frame and read by nothing: the counters that matter to a
# turn are on its `result`.
_ASSISTANT_USAGE: dict[str, Any] = {
    "cache_creation": {"ephemeral_1h_input_tokens": 0},
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 21_507,
    "inference_geo": "us",
    "input_tokens": 4,
    "output_tokens": 91,
    "service_tier": "standard",
}


def text_block(text: str) -> dict[str, Any]:
    return {"text": text, "type": "text"}


def thinking_block(thinking: str) -> dict[str, Any]:
    return {"signature": "EqQBCkYIBxgCK", "thinking": thinking, "type": "thinking"}


def tool_use_block(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"caller": {"type": "direct"}, "id": call_id, "input": dict(arguments), "name": name, "type": "tool_use"}


def assistant(
    *blocks: dict[str, Any], message_id: str | None = None, parent_tool_use_id: str | None = None
) -> dict[str, Any]:
    """One frame of an answer. Production sends one content block per frame, so several blocks
    here is a shape the wire does not produce — pass one, and a second frame for the next.

    `parent_tool_use_id` is the call a nested frame belongs to. `protocol.md` describes it as the
    marker of a subagent's forwarded frames. Passing it is a compatibility shape, so tests using
    it make that assumption explicit.
    """
    message: dict[str, Any] = {
        "content": list(blocks),
        "context_management": None,
        "diagnostics": None,
        "model": "claude-opus-4-6-20260514",
        "role": "assistant",
        "stop_details": None,
        # Nothing in an `assistant` frame says the message is finished.
        "stop_reason": None,
        "stop_sequence": None,
        "type": "message",
        "usage": _ASSISTANT_USAGE,
    }
    if message_id is not None:
        message["id"] = message_id
    return {
        "message": message,
        "parent_tool_use_id": parent_tool_use_id,
        "request_id": "req_011CX",
        "session_id": SESSION_ID,
        "timestamp": "2026-08-15T06:12:04.113Z",
        "type": "assistant",
    }


def tool_result(
    call_id: str,
    content: Any,
    *,
    structured: Any = None,
    is_error: bool | None = None,
    parent_tool_use_id: str | None = None,
) -> dict[str, Any]:
    """An inbound `user` frame: what a tool answered.

    `is_error=None` models a result that omits the key entirely, which is why the key is absent
    rather than false. `structured` is the top-level `tool_use_result` the real output
    rides on, beside the renderable `content`.
    """
    block: dict[str, Any] = {"content": content, "tool_use_id": call_id, "type": "tool_result"}
    if is_error is not None:
        block["is_error"] = is_error
    return {
        "message": {"content": [block], "role": "user"},
        "parent_tool_use_id": parent_tool_use_id,
        "session_id": SESSION_ID,
        "tool_use_result": structured,
        "type": "user",
    }


def tool_progress(
    call_id: str, tool_name: str, *, parent_tool_use_id: str, elapsed_time_seconds: int
) -> dict[str, Any]:
    """A long-running tool saying it is still running.

    Absent from `protocol.md` entirely, so nothing in the fold has a case for it.
    """
    return {
        "elapsed_time_seconds": elapsed_time_seconds,
        "heartbeat": True,
        "parent_tool_use_id": parent_tool_use_id,
        "session_id": SESSION_ID,
        "tool_name": tool_name,
        "tool_use_id": call_id,
        "type": "tool_progress",
    }


def prompt(text: str) -> dict[str, Any]:
    """An outbound prompt: content is a string on 121 of 121, which is what says which way it
    went — the CLI sends `user` frames too, carrying tool results."""
    return {"message": {"content": text, "role": "user"}, "parent_tool_use_id": None, "type": "user"}


def text_delta(text: str) -> dict[str, Any]:
    return stream_event({"delta": {"text": text, "type": "text_delta"}, "index": 0, "type": "content_block_delta"})


def tool_use_start(call_id: str, name: str, *, index: int = 0) -> dict[str, Any]:
    """The streaming declaration that precedes a tool's argument fragments."""
    return stream_event(
        {"content_block": tool_use_block(call_id, name, {}), "index": index, "type": "content_block_start"}
    )


def content_block_stop(*, index: int = 0) -> dict[str, Any]:
    return stream_event({"index": index, "type": "content_block_stop"})


def input_json_delta(partial_json: str, *, index: int = 0) -> dict[str, Any]:
    """A tool's arguments arriving a fragment at a time — 863 of 950 production deltas."""
    return stream_event(
        {
            "delta": {"partial_json": partial_json, "type": "input_json_delta"},
            "index": index,
            "type": "content_block_delta",
        }
    )


def stream_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, "type": "stream_event"}


# Sample `result` accounting. Read by nothing — the console keeps no account of what an exchange
# cost — and emitted because every result frame carries it.
_RESULT_ACCOUNTING: dict[str, Any] = {
    "duration_ms": 41_902,
    "total_cost_usd": 0.4213,
    "usage": {"cache_read_input_tokens": 133_907, "input_tokens": 19, "output_tokens": 1_204},
}


def result(
    *, text: str = "", subtype: str = "success", uuid: str | None = None, is_error: bool = False
) -> dict[str, Any]:
    """The frame that ends a turn.

    `is_error` is false on all 129 production results, including 27 sessions the console recorded
    as failed, so `subtype` is the CLI's only statement about how the turn went. It is a parameter
    anyway, for the tests that prove nothing reads it.
    """
    frame: dict[str, Any] = {
        "api_error_status": None,
        "duration_api_ms": 41_388,
        "is_error": is_error,
        "num_turns": 7,
        "permission_denials": [],
        "result": text,
        "session_id": SESSION_ID,
        "stop_reason": "end_turn",
        "subtype": subtype,
        "terminal_reason": "completed",
        "type": "result",
        **_RESULT_ACCOUNTING,
    }
    if uuid is not None:
        frame["uuid"] = uuid
    return frame


def system(subtype: str, **fields: Any) -> dict[str, Any]:
    """73% of frames are `system`, and the subtype is the whole of what distinguishes them."""
    return {"session_id": SESSION_ID, "subtype": subtype, "type": "system"} | fields


def heartbeat() -> dict[str, Any]:
    """`thinking_tokens` — 8,512 frames, 56% of everything recorded."""
    return system("thinking_tokens", estimated_tokens=1_024, estimated_tokens_delta=31)


def command_lifecycle(command_uuid: str, state: str) -> dict[str, Any]:
    return {"command_uuid": command_uuid, "session_id": SESSION_ID, "state": state, "type": "command_lifecycle"}
