"""Codex model history recovery; app-server turn items are a different projection."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel

from agentplane.protocol import event_pb2
from agentplane.runner.recovery import ObservedItem

# gazelle:include_dep @pypi//protobuf


class FunctionCall(BaseModel):
    call_id: str


class FunctionOutput(BaseModel):
    call_id: str
    output: str


class MessageContent(BaseModel):
    type: str
    text: str


class ModelMessage(BaseModel):
    id: str
    role: str
    content: list[MessageContent]


class ReasoningSummaryPart(BaseModel):
    text: str


class ModelReasoning(BaseModel):
    # Codex 0.157.0 uses the app-server reasoning item id here, so observed items match by it.
    id: str
    summary: list[ReasoningSummaryPart]


def read_history(directory: Path, session_id: str, observed: dict[str, ObservedItem]) -> dict[str, ObservedItem] | None:
    paths = list((directory / "sessions").glob(f"**/*{session_id}.jsonl"))
    if len(paths) != 1:
        return None
    texts: dict[str, str] = {}
    summaries: dict[str, str] = {}
    calls: set[str] = set()
    outputs: dict[str, str] = {}
    with paths[0].open() as source:
        for line in source:
            record = json.loads(line)
            if record["type"] == "compacted":
                # Reconstructing a compaction's replacement history needs its own adapter.
                return None
            if record["type"] == "event_msg" and record["payload"].get("type") == "thread_rolled_back":
                return None
            if record["type"] != "response_item":
                continue
            payload = record["payload"]
            match payload["type"]:
                case "message" if payload.get("role") == "assistant":
                    message = ModelMessage.model_validate(payload)
                    texts[message.id] = "".join(part.text for part in message.content)
                case "reasoning":
                    reasoning = ModelReasoning.model_validate(payload)
                    summaries[reasoning.id] = "\n".join(part.text for part in reasoning.summary)
                case "function_call":
                    call = FunctionCall.model_validate(payload)
                    calls.add(call.call_id)
                case "function_call_output":
                    result = FunctionOutput.model_validate(payload)
                    outputs[result.call_id] = result.output
    recovered: dict[str, ObservedItem] = {}
    for item_id, item in observed.items():
        if item.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT:
            if item_id in texts:
                recovered[item_id] = replace(item, text=texts[item_id])
        elif item.kind == event_pb2.ITEM_KIND_TOOL_CALL:
            if item_id in calls:
                # core/context_manager/normalize.rs at rust-v0.157.0 inserts "aborted"
                # for a function call without output. This is model input, not execution evidence.
                output = outputs.get(item_id, "aborted")
                if item.completed and item.output in output:
                    # Shell model results wrap the observed stdout in execution metadata.
                    output = item.output
                recovered[item_id] = replace(item, output=output)
        elif item.kind == event_pb2.ITEM_KIND_REASONING:
            # Only a saved record vouches for reasoning; observing it complete does not.
            if item_id in summaries:
                recovered[item_id] = replace(item, text=summaries[item_id])
        elif item.completed:
            recovered[item_id] = item
    return recovered
