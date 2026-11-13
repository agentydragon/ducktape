"""Read and write canonical ThreadEntity ProtoJSON from the indexed storage columns."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message

from agentplane.history_service.api.history import history_service_pb2

_VARIANT_BY_KIND = {
    "view_state": "viewState",
    "item": "item",
    "confirmed_input": "confirmedInput",
    "lifecycle": "lifecycle",
    "command": "command",
}


def thread_entity_from_record(record: Mapping[str, Any] | object) -> history_service_pb2.ThreadEntity:
    """Parse one stored row or Electric row into its generated, typed entity message."""
    get = record.get if isinstance(record, Mapping) else lambda name: getattr(record, name)
    entity_kind = str(get("entity_kind"))
    try:
        variant = _VARIANT_BY_KIND[entity_kind]
    except KeyError as error:
        raise ValueError(f"unknown stored ThreadEntity kind {entity_kind!r}") from error

    state = get("state")
    if isinstance(state, str):
        state = json.loads(state)
    if not isinstance(state, Mapping):
        raise ValueError(f"stored ThreadEntity state must be an object, got {type(state).__name__}")

    proto_json = {
        "sessionId": str(get("thread_id")),
        "projectionEpoch": str(get("projection_epoch")),
        "entityId": str(get("entity_id")),
        "entityIndex": str(get("entity_index")),
        "cursor": str(get("cursor")),
        "revisionCursor": str(get("revision_cursor")),
        variant: state,
    }
    return ParseDict(proto_json, history_service_pb2.ThreadEntity())


def thread_entity_variant(entity: history_service_pb2.ThreadEntity) -> Message:
    """Return the oneof submessage stored in the row's ProtoJSON ``state`` column."""
    variant = entity.WhichOneof("entity")
    if variant is None:
        raise ValueError("ThreadEntity has no entity variant")
    value = getattr(entity, variant)
    if not isinstance(value, Message):
        raise TypeError(f"ThreadEntity variant {variant!r} is not a message")
    return value


def thread_entity_state_json(entity: history_service_pb2.ThreadEntity) -> dict[str, Any]:
    """Encode only the canonical variant ProtoJSON stored in the indexed row's ``state`` column."""
    return MessageToDict(thread_entity_variant(entity))
