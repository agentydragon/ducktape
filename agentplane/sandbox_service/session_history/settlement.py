"""Decide whether an item field's streamed deltas can leave stored history.

`settle` is a pure function of one item's span of runner entries, ending at the entry that
completes the field. It settles only when every streamed delta of that field is a pair, a native
frame matching an exact template and the runner's derived delta made from it, and the retained
native completion frame holds exactly their concatenation. Any doubt keeps every entry: what a
settlement drops (chunking, per-chunk timestamps and ids, frame serialization) is all it drops.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from agentplane.native.delta_templates import ClaudeDelta, CodexDelta, DeltaTemplate, match_delta, parse_strict
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service import protocol_pb2

# The generated protobuf stubs need the protobuf runtime as a direct mypy dependency.
# gazelle:include_dep @pypi//protobuf


class Field(StrEnum):
    TEXT = "text"
    ARGUMENTS = "arguments"
    OUTPUT = "output"


@dataclass(frozen=True)
class Target:
    item_id: str
    field: Field


# Observations that may signal a harness or runner problem. A span holding one keeps every packet,
# so the frames stay available to turn into a harness/runner test.
_ANOMALIES = frozenset(
    {
        "command_failed",
        "harness_stderr",
        "harness_lost",
        "harness_exited",
        "harness_launch_failed",
        "conversation_reconciled",
        "turn_completed",
    }
)

_TEMPLATES: dict[Field, frozenset[DeltaTemplate]] = {
    Field.TEXT: frozenset(
        {
            DeltaTemplate.CLAUDE_TEXT,
            DeltaTemplate.CLAUDE_THINKING,
            DeltaTemplate.CODEX_AGENT_MESSAGE,
            DeltaTemplate.CODEX_REASONING_SUMMARY,
        }
    ),
    Field.ARGUMENTS: frozenset({DeltaTemplate.CLAUDE_INPUT_JSON}),
    Field.OUTPUT: frozenset({DeltaTemplate.CODEX_COMMAND_OUTPUT}),
}


def completion_target(entry: event_log_pb2.EventEntry) -> Target | None:
    """The item field whose content this entry completes, if any."""
    event = entry.event
    match event.WhichOneof("observation"):
        case "item_completed":
            completed = event.item_completed
            return Target(completed.item_id, Field.TEXT if completed.HasField("text") else Field.OUTPUT)
        case "tool_arguments":
            return Target(event.tool_arguments.item_id, Field.ARGUMENTS)
    return None


def started_item(entry: event_log_pb2.EventEntry) -> str | None:
    return entry.event.item_started.item_id if entry.event.HasField("item_started") else None


def _delta(entry: event_log_pb2.EventEntry, target: Target) -> str | None:
    """The content of the target's derived delta Event, or None for any other entry."""
    event = entry.event
    match (event.WhichOneof("observation"), target.field):
        case ("text_delta", Field.TEXT) if event.text_delta.item_id == target.item_id:
            return event.text_delta.text
        case ("tool_arguments_delta", Field.ARGUMENTS) if event.tool_arguments_delta.item_id == target.item_id:
            return event.tool_arguments_delta.partial_json
        case ("tool_output_delta", Field.OUTPUT) if event.tool_output_delta.item_id == target.item_id:
            return event.tool_output_delta.text
    return None


def _native_frame(entry: event_log_pb2.EventEntry | None) -> dict[str, object] | None:
    if (
        entry is None
        or not entry.event.HasField("native")
        or entry.event.native.direction != event_pb2.DIRECTION_FROM_HARNESS
    ):
        return None
    return parse_strict(entry.event.native.line)


def _one_source(entry: event_log_pb2.EventEntry) -> int | None:
    sources = entry.event.source_sequences
    return sources[0] if len(sources) == 1 else None


def settle(span: Sequence[event_log_pb2.EventEntry]) -> protocol_pb2.SettledDeltas | None:
    """`span` runs in cursor order from the item's ItemStarted (and its native source) through the
    completing entry, which is last. Stored gaps from earlier settlements are allowed."""
    if not span:
        return None
    completion = span[-1]
    target = completion_target(completion)
    if target is None:
        return None
    by_cursor = {entry.cursor: entry for entry in span}
    for entry in span:
        observation = entry.event.WhichOneof("observation")
        if observation in _ANOMALIES:
            return None
        if (
            observation == "native"
            and entry.event.native.direction == event_pb2.DIRECTION_FROM_HARNESS
            and parse_strict(entry.event.native.line) is None
        ):
            return None
    completion_source = _one_source(completion)
    completion_frame = _native_frame(by_cursor.get(completion_source)) if completion_source is not None else None
    if completion_frame is None:
        return None
    starts = [entry for entry in span if started_item(entry) == target.item_id]
    if len(starts) != 1:
        return None
    start_source = _one_source(starts[0])
    start_frame = _native_frame(by_cursor.get(start_source)) if start_source is not None else None

    pairs: list[tuple[event_log_pb2.EventEntry, int, ClaudeDelta | CodexDelta]] = []
    for entry in span:
        content = _delta(entry, target)
        if content is None:
            continue
        source = _one_source(entry)
        frame = _native_frame(by_cursor.get(source)) if source is not None else None
        matched = match_delta(frame) if frame is not None else None
        if source is None or matched is None or matched.content != content:
            return None
        if matched.template not in _TEMPLATES[target.field]:
            return None
        pairs.append((entry, source, matched))
    if not pairs or len({matched.template for _, _, matched in pairs}) != 1:
        return None
    template = pairs[0][2].template

    elided = {entry.cursor for entry, _, _ in pairs} | {source for _, source, _ in pairs}
    # A removed native frame must be evidence for its own delta only.
    for entry in span:
        if entry.cursor not in elided and elided.intersection(entry.event.source_sequences):
            return None
    deltas = [matched for _, _, matched in pairs]
    if not _identities_agree(deltas, target, completion_frame, start_frame):
        return None
    if not _completion_holds(deltas, target, completion, completion_frame):
        return None

    settlement = protocol_pb2.SettledDeltas(
        item_id=target.item_id,
        template=template,
        completion_cursor=completion.cursor,
        ranges=[protocol_pb2.CursorRange(first=first, last=last) for first, last in cursor_ranges(elided)],
        chunk_count=len(pairs),
    )
    settlement.first_chunk_at.CopyFrom(pairs[0][0].event.at)
    settlement.last_chunk_at.CopyFrom(pairs[-1][0].event.at)
    return settlement


def cursor_ranges(cursors: set[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for cursor in sorted(cursors):
        if ranges and ranges[-1][1] == cursor - 1:
            ranges[-1] = (ranges[-1][0], cursor)
        else:
            ranges.append((cursor, cursor))
    return ranges


def _identities_agree(
    deltas: Sequence[ClaudeDelta | CodexDelta],
    target: Target,
    completion: dict[str, object],
    start: dict[str, object] | None,
) -> bool:
    if all(isinstance(delta, ClaudeDelta) for delta in deltas):
        # Claude names the block by its index in the streamed message, which the item's
        # content_block_start established.
        event = start.get("event") if start is not None and start.get("type") == "stream_event" else None
        if not isinstance(event, dict) or event.get("type") != "content_block_start":
            return False
        return all(
            isinstance(delta, ClaudeDelta)
            and delta.session_id == completion.get("session_id")
            and delta.parent_tool_use_id == completion.get("parent_tool_use_id")
            and delta.index == event.get("index")
            for delta in deltas
        )
    params = completion.get("params")
    if not isinstance(params, dict):
        return False
    return all(
        isinstance(delta, CodexDelta)
        and delta.item_id == target.item_id
        and delta.thread_id == params.get("threadId")
        and delta.turn_id == params.get("turnId")
        for delta in deltas
    )


def _completion_holds(
    deltas: Sequence[ClaudeDelta | CodexDelta],
    target: Target,
    completion: event_log_pb2.EventEntry,
    frame: dict[str, object],
) -> bool:
    """The retained completion frame, and the runner's completion Event, hold exactly the
    concatenation."""
    text = "".join(delta.content for delta in deltas)
    template = deltas[0].template
    match template:
        case DeltaTemplate.CLAUDE_TEXT | DeltaTemplate.CLAUDE_THINKING:
            key = "text" if template is DeltaTemplate.CLAUDE_TEXT else "thinking"
            return completion.event.item_completed.text == text and any(
                block.get(key) == text for block in _claude_blocks(frame, target.item_id)
            )
        case DeltaTemplate.CLAUDE_INPUT_JSON:
            parsed = parse_strict(text) if text.lstrip().startswith("{") else None
            if parsed is None:
                return False
            return json.loads(completion.event.tool_arguments.arguments_json) == parsed and any(
                block.get("type") == "tool_use" and block.get("id") == target.item_id and block.get("input") == parsed
                for block in _claude_blocks(frame, target.item_id)
            )
        case DeltaTemplate.CODEX_AGENT_MESSAGE:
            item = _codex_item(frame, target.item_id, "agentMessage")
            return item is not None and item.get("text") == text == completion.event.item_completed.text
        case DeltaTemplate.CODEX_REASONING_SUMMARY:
            item = _codex_item(frame, target.item_id, "reasoning")
            summary = item.get("summary") if item is not None else None
            if not isinstance(summary, list):
                return False
            parts: defaultdict[int, list[str]] = defaultdict(list)
            for delta in deltas:
                if not isinstance(delta, CodexDelta) or delta.summary_index is None:
                    return False
                parts[delta.summary_index].append(delta.content)
            return all(index < len(summary) and summary[index] == "".join(part) for index, part in parts.items())
        case DeltaTemplate.CODEX_COMMAND_OUTPUT:
            item = _codex_item(frame, target.item_id, "commandExecution")
            return (
                item is not None and item.get("aggregatedOutput") == text == completion.event.item_completed.tool.output
            )


def _claude_blocks(frame: dict[str, object], item_id: str) -> list[dict[str, object]]:
    """The content blocks of the retained `assistant` frame for this item's message."""
    message = frame.get("message")
    if frame.get("type") != "assistant" or not isinstance(message, dict):
        return []
    content = message.get("content")
    # Text and thinking items are `<message id>#<block index>`; tool calls are the tool_use id.
    if "#" in item_id and item_id.rsplit("#", 1)[0] != message.get("id"):
        return []
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _codex_item(frame: dict[str, object], item_id: str, item_type: str) -> dict[str, object] | None:
    params = frame.get("params")
    item = params.get("item") if frame.get("method") == "item/completed" and isinstance(params, dict) else None
    if not isinstance(item, dict) or item.get("id") != item_id or item.get("type") != item_type:
        return None
    return item
