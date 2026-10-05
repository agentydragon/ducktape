"""Read Claude's persisted main conversation chain, preserving native block identities."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from agentplane.native.claude.blocks import Block, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, blocks_of
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf
from agentplane.runner.recovery import ObservedItem

# The block types Claude's resume loader treats as thinking.
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


class TranscriptMessage(BaseModel):
    role: str
    content: str | list[Block]
    id: str | None = None


class TranscriptEntry(BaseModel):
    type: str
    uuid: str
    parent_uuid: str | None = Field(alias="parentUuid")
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    message: TranscriptMessage | None = None


def _holds_only_thinking(message: TranscriptMessage) -> bool:
    blocks = blocks_of(message.content)
    return bool(blocks) and all(block.type in _THINKING_TYPES for block in blocks)


def read_history(directory: Path, session_id: str) -> dict[str, ObservedItem]:
    """The items Claude's resume loads from its persisted transcript.

    Raises ValueError where the transcript cannot say what a resume would load.
    """
    paths = list((directory / "projects").glob(f"*/{session_id}.jsonl"))
    if len(paths) != 1:
        raise ValueError(f"expected one Claude transcript for {session_id=}, found {len(paths)}")
    entries: dict[str, TranscriptEntry] = {}
    leaf: TranscriptEntry | None = None
    with paths[0].open() as source:
        for line in source:
            value = json.loads(line)
            if value["type"] == "system" and value.get("subtype") == "compact_boundary":
                raise ValueError("Claude compacted this conversation, so items before the boundary cannot be compared")
            if "uuid" not in value or "parentUuid" not in value:
                continue
            if value["type"] not in {"user", "assistant"}:
                value = {**value, "message": None}
            entry = TranscriptEntry.model_validate(value)
            entries[entry.uuid] = entry
            if entry.type in {"user", "assistant"} and not entry.is_sidechain:
                leaf = entry
    chain: list[TranscriptEntry] = []
    visited: set[str] = set()
    while leaf is not None:
        if leaf.uuid in visited:
            raise ValueError("cycle in Claude's persisted conversation")
        visited.add(leaf.uuid)
        chain.append(leaf)
        if leaf.parent_uuid is None:
            break
        leaf = entries.get(leaf.parent_uuid)
        if leaf is None:
            raise ValueError("Claude's persisted conversation has an entry whose parent is missing")
    # Claude's resume deserializer removes assistant messages whose tool calls have no matching
    # result, then a message holding only thinking once no other entry of its message id is left
    # (each block is its own entry). Presence in the append-only transcript alone is not continuation.
    resolved = {
        block.tool_use_id
        for entry in chain
        if entry.message is not None
        for block in blocks_of(entry.message.content)
        if isinstance(block, ToolResultBlock)
    }
    # Each surviving message with the index of its first block within its native message; a removed
    # entry still advances that index.
    kept: list[tuple[TranscriptMessage, int]] = []
    block_counts: dict[str, int] = {}
    for entry in reversed(chain):
        if entry.message is None:
            continue
        message = entry.message
        blocks = blocks_of(message.content)
        offset = block_counts.get(message.id or "", 0)
        if message.id is not None:
            block_counts[message.id] = offset + len(blocks)
        calls = [block.id for block in blocks if isinstance(block, ToolUseBlock)]
        if not (calls and all(call not in resolved for call in calls)):
            kept.append((message, offset))
    answered = {message.id for message, _ in kept if not _holds_only_thinking(message)}
    items: dict[str, ObservedItem] = {}
    for message, offset in kept:
        if _holds_only_thinking(message) and message.id not in answered:
            continue
        for index, block in enumerate(blocks_of(message.content), start=offset):
            match block:
                case TextBlock(text=text) if message.role == "assistant" and message.id is not None:
                    item_id = f"{message.id}#{index}"
                    items[item_id] = ObservedItem(item_id, event_pb2.ITEM_KIND_ASSISTANT_TEXT, text=text)
                case ThinkingBlock(thinking=text) if message.id is not None:
                    item_id = f"{message.id}#{index}"
                    items[item_id] = ObservedItem(item_id, event_pb2.ITEM_KIND_REASONING, text=text)
                case ToolUseBlock(id=item_id, input=arguments):
                    items[item_id] = ObservedItem(
                        item_id, event_pb2.ITEM_KIND_TOOL_CALL, arguments=json.dumps(arguments)
                    )
                case ToolResultBlock(tool_use_id=item_id) if item_id in items:
                    items[item_id].output = block.text
                    items[item_id].completed = True
    return items
