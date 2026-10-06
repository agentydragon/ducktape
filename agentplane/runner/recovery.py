"""Compare observed items with native continuation evidence at an interruption boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf
from agentplane.runner.journal import Journal


@dataclass
class ObservedItem:
    item_id: str
    kind: int
    text: str = ""
    arguments: str = ""
    output: str = ""
    completed: bool = False
    tool_name: str = ""


async def observed_items(journal: Journal, turn_id: str) -> dict[str, ObservedItem]:
    items: dict[str, ObservedItem] = {}
    async for entry in journal.turn_events(turn_id):
        event = entry.event
        match event.WhichOneof("observation"):
            case "item_started":
                started = event.item_started
                items[started.item_id] = ObservedItem(started.item_id, started.kind, tool_name=started.tool_name)
            case "text_delta":
                items[event.text_delta.item_id].text += event.text_delta.text
            case "tool_arguments_delta":
                items[event.tool_arguments_delta.item_id].arguments += event.tool_arguments_delta.partial_json
            case "tool_arguments":
                items[event.tool_arguments.item_id].arguments = event.tool_arguments.arguments_json
            case "tool_output_delta":
                items[event.tool_output_delta.item_id].output += event.tool_output_delta.text
            case "item_completed":
                completed = event.item_completed
                item = items[completed.item_id]
                item.completed = True
                if completed.HasField("text"):
                    item.text = completed.text
                else:
                    item.output = completed.tool.output
    return items


def compare_item(observed: ObservedItem, recovered: ObservedItem | None) -> event_pb2.ItemRecovery:
    if recovered is None:
        disposition = event_pb2.RECOVERY_DISPOSITION_ABSENT
        reason = ""
    elif (
        observed.kind == recovered.kind
        and observed.text == recovered.text
        and _arguments_equal(observed.arguments, recovered.arguments)
        and observed.output == recovered.output
    ):
        disposition = event_pb2.RECOVERY_DISPOSITION_RETAINED
        reason = ""
    else:
        return event_pb2.ItemRecovery(
            item_id=observed.item_id,
            disposition=event_pb2.RECOVERY_DISPOSITION_REVISED,
            replacement=event_pb2.RecoveredContent(
                text=recovered.text, arguments_json=recovered.arguments, output=recovered.output
            ),
        )
    return event_pb2.ItemRecovery(item_id=observed.item_id, disposition=disposition, reason=reason)


def _arguments_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        return bool(json.loads(left) == json.loads(right))
    except json.JSONDecodeError:
        return False


def unknown_item(item_id: str, reason: str) -> event_pb2.ItemRecovery:
    return event_pb2.ItemRecovery(item_id=item_id, disposition=event_pb2.RECOVERY_DISPOSITION_UNKNOWN, reason=reason)


def unknown_report(turn_id: str, items: dict[str, ObservedItem], reason: str) -> event_pb2.ConversationReconciled:
    return event_pb2.ConversationReconciled(turn_id=turn_id, items=[unknown_item(item_id, reason) for item_id in items])
