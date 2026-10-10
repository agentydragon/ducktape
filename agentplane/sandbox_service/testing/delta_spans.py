"""Runner journal spans of one streamed item, shaped like the staging frames the templates match."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from agentplane.protocol import event_log_pb2, event_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf

SOURCE = "test-source"
CLAUDE_SESSION = "test-claude-session"
CODEX_THREAD = "test-codex-thread"
CODEX_TURN = "test-codex-turn"


@dataclass
class Span:
    entries: list[event_log_pb2.EventEntry] = field(default_factory=list)

    def _next(self) -> event_log_pb2.EventEntry:
        cursor = len(self.entries) + 1
        entry = event_log_pb2.EventEntry(cursor=cursor)
        entry.origin.source_id = SOURCE
        entry.origin.sequence = cursor
        entry.event.at.FromMilliseconds(1_000 * cursor)
        self.entries.append(entry)
        return entry

    def native(self, frame: dict[str, object] | str) -> int:
        entry = self._next()
        entry.event.native.direction = event_pb2.DIRECTION_FROM_HARNESS
        entry.event.native.line = frame if isinstance(frame, str) else json.dumps(frame)
        return entry.cursor

    def derived(self, sources: list[int], event: event_pb2.Event) -> int:
        entry = self._next()
        entry.event.MergeFrom(event)
        entry.event.source_sequences.extend(sources)
        return entry.cursor

    def stderr(self, line: str) -> int:
        entry = self._next()
        entry.event.harness_stderr.text = line
        return entry.cursor


def claude_frame(event: dict[str, object], uuid: str) -> dict[str, object]:
    return {
        "type": "stream_event",
        "event": event,
        "session_id": CLAUDE_SESSION,
        "parent_tool_use_id": None,
        "uuid": uuid,
    }


def claude_delta(index: int, delta: dict[str, object], uuid: str) -> dict[str, object]:
    return claude_frame({"type": "content_block_delta", "index": index, "delta": delta}, uuid)


def claude_assistant(message_id: str, block: dict[str, object]) -> dict[str, object]:
    return {
        "type": "assistant",
        "message": {"id": message_id, "type": "message", "role": "assistant", "content": [block]},
        "session_id": CLAUDE_SESSION,
        "parent_tool_use_id": None,
        "uuid": "test-assistant-frame",
    }


def claude_text(
    chunks: list[str], *, completed: str | None = None, interpose: Callable[[Span], object] | None = None
) -> Span:
    """A Claude text block streamed as `chunks`; `completed` overrides what the completion holds, and
    `interpose` appends other entries before the completion."""
    text = "".join(chunks) if completed is None else completed
    span = Span()
    start = span.native(
        claude_frame(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "test-start"
        )
    )
    span.derived(
        [start],
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="msg_test#0", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        ),
    )
    for number, chunk in enumerate(chunks):
        frame = span.native(claude_delta(0, {"type": "text_delta", "text": chunk}, f"test-chunk-{number}"))
        span.derived([frame], event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="msg_test#0", text=chunk)))
    if interpose is not None:
        interpose(span)
    assistant = span.native(claude_assistant("msg_test", {"type": "text", "text": text}))
    span.derived([assistant], event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="msg_test#0", text=text)))
    return span


def claude_tool_arguments(chunks: list[str], tool_input: dict[str, object]) -> Span:
    span = Span()
    start = span.native(
        claude_frame(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_test", "name": "Bash", "input": {}},
            },
            "test-start",
        )
    )
    span.derived(
        [start],
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(
                item_id="toolu_test", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="Bash"
            )
        ),
    )
    for number, chunk in enumerate(chunks):
        frame = span.native(
            claude_delta(1, {"type": "input_json_delta", "partial_json": chunk}, f"test-chunk-{number}")
        )
        span.derived(
            [frame],
            event_pb2.Event(
                tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="toolu_test", partial_json=chunk)
            ),
        )
    assistant = span.native(
        claude_assistant("msg_test", {"type": "tool_use", "id": "toolu_test", "name": "Bash", "input": tool_input})
    )
    span.derived(
        [assistant],
        event_pb2.Event(
            tool_arguments=event_pb2.ToolArguments(item_id="toolu_test", arguments_json=json.dumps(tool_input))
        ),
    )
    return span


def codex_params(item_id: str, delta: str, **extra: object) -> dict[str, object]:
    return {"threadId": CODEX_THREAD, "turnId": CODEX_TURN, "itemId": item_id, "delta": delta, **extra}


def codex_frame(method: str, params: dict[str, object]) -> dict[str, object]:
    return {"method": method, "params": params, "emittedAtMs": 1_760_000_000_000}


def codex_completed(item: dict[str, object]) -> dict[str, object]:
    return codex_frame(
        "item/completed",
        {"item": item, "threadId": CODEX_THREAD, "turnId": CODEX_TURN, "completedAtMs": 1_760_000_000_001},
    )


def codex_reasoning(parts: list[list[str]]) -> Span:
    """A Codex reasoning item whose summary part `i` streams as the chunks `parts[i]`."""
    span = Span()
    item = {"type": "reasoning", "id": "rs_test", "summary": [], "content": []}
    start = span.native(
        codex_frame("item/started", {"item": item, "threadId": CODEX_THREAD, "turnId": CODEX_TURN, "startedAtMs": 1})
    )
    span.derived(
        [start],
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="rs_test", kind=event_pb2.ITEM_KIND_REASONING)),
    )
    for index, chunks in enumerate(parts):
        for chunk in chunks:
            frame = span.native(
                codex_frame("item/reasoning/summaryTextDelta", codex_params("rs_test", chunk, summaryIndex=index))
            )
            span.derived([frame], event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="rs_test", text=chunk)))
    summary = ["".join(chunks) for chunks in parts]
    completed = span.native(codex_completed({**item, "summary": summary}))
    span.derived(
        [completed], event_pb2.Event(item_completed=event_pb2.ItemCompleted(item_id="rs_test", text="\n".join(summary)))
    )
    return span


def codex_command(chunks: list[str], *, aggregated: str | None) -> Span:
    span = Span()
    item = {"type": "commandExecution", "id": "call_test", "command": "true", "status": "inProgress"}
    start = span.native(
        codex_frame("item/started", {"item": item, "threadId": CODEX_THREAD, "turnId": CODEX_TURN, "startedAtMs": 1})
    )
    span.derived(
        [start],
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(
                item_id="call_test", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="commandExecution"
            )
        ),
    )
    for chunk in chunks:
        frame = span.native(codex_frame("item/commandExecution/outputDelta", codex_params("call_test", chunk)))
        span.derived(
            [frame], event_pb2.Event(tool_output_delta=event_pb2.ToolOutputDelta(item_id="call_test", text=chunk))
        )
    completed = span.native(
        codex_completed({**item, "status": "completed", "aggregatedOutput": aggregated, "exitCode": 0})
    )
    span.derived(
        [completed],
        event_pb2.Event(
            item_completed=event_pb2.ItemCompleted(
                item_id="call_test", tool=event_pb2.ToolResult(output=aggregated or "", succeeded=True)
            )
        ),
    )
    return span
