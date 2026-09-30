"""Read Claude's persisted main conversation chain, preserving native block identities."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from agentplane.native.claude.blocks import Block, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock, blocks_of
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf
from agentplane.runner.recovery import ObservedItem


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


def read_history(directory: Path, session_id: str) -> dict[str, ObservedItem] | None:
    paths = list((directory / "projects").glob(f"*/{session_id}.jsonl"))
    if len(paths) != 1:
        return None
    entries: dict[str, TranscriptEntry] = {}
    leaf: TranscriptEntry | None = None
    with paths[0].open() as source:
        for line in source:
            value = json.loads(line)
            if value["type"] == "system" and value.get("subtype") == "compact_boundary":
                return None
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
            return None
    # Claude's resume deserializer removes assistant messages whose tool calls have no
    # matching result. Presence in the append-only transcript alone is not continuation.
    resolved = {
        block.tool_use_id
        for entry in chain
        if entry.message is not None
        for block in blocks_of(entry.message.content)
        if isinstance(block, ToolResultBlock)
    }
    items: dict[str, ObservedItem] = {}
    block_counts: dict[str, int] = {}
    for entry in reversed(chain):
        if entry.message is None:
            continue
        message = entry.message
        blocks = list(blocks_of(message.content))
        offset = block_counts.get(message.id or "", 0)
        if message.id is not None:
            block_counts[message.id] = offset + len(blocks)
        calls = [block.id for block in blocks if isinstance(block, ToolUseBlock)]
        if calls and all(call not in resolved for call in calls):
            continue
        for index, block in enumerate(blocks, start=offset):
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
