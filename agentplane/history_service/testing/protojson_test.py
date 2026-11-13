"""The indexed row and its variant ``state`` column compose to canonical ProtoJSON."""

import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict

from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.testing.protojson import (
    thread_entity_from_record,
    thread_entity_state_json,
    thread_entity_variant,
)
from agentplane.proto import command_pb2

_UINT64_MAX = (1 << 64) - 1
_REFERENCE = {
    "projectionEpoch": "epoch",
    "ownerCursor": str(_UINT64_MAX - 1),
    "ownerId": "owner",
    "field": "THREAD_PAYLOAD_FIELD_TEXT",
    "revisionCursor": str(_UINT64_MAX - 2),
    "generation": str(_UINT64_MAX - 3),
    "chunkCount": "1",
}


@pytest.mark.parametrize(
    ("kind", "variant", "state"),
    [
        ("view_state", "view_state", {"controls": {"appliedModel": "model"}}),
        ("item", "item", {"kind": "ITEM_KIND_ASSISTANT_TEXT", "text": {}, "turnId": "turn", "textRef": _REFERENCE}),
        (
            "confirmed_input",
            "confirmed_input",
            {"harnessMessageId": "message", "originCommandIds": ["command"], "turnId": "turn", "inputRef": _REFERENCE},
        ),
        ("lifecycle", "lifecycle", {"event": {"turnStarted": {"turnId": "turn", "model": "model"}}}),
        (
            "command",
            "command",
            {
                "operation": "OPERATION_SUBMIT_INPUT",
                "outcome": "THREAD_COMMAND_OUTCOME_EFFECTED",
                "outcomeCursor": str(_UINT64_MAX - 4),
                "inputRef": _REFERENCE,
            },
        ),
    ],
)
def test_thread_entity_from_record_parses_each_flattened_variant(
    kind: str, variant: str, state: dict[str, object]
) -> None:
    record = {
        "thread_id": "session",
        "projection_epoch": "epoch",
        "entity_kind": kind,
        "entity_id": "entity",
        "entity_index": str(_UINT64_MAX),
        "cursor": str(_UINT64_MAX - 1),
        "revision_cursor": str(_UINT64_MAX - 2),
        "state": state,
    }

    entity = thread_entity_from_record(record)

    assert entity.WhichOneof("entity") == variant
    assert (entity.session_id, entity.projection_epoch, entity.entity_id) == ("session", "epoch", "entity")
    assert entity.entity_index == _UINT64_MAX
    assert entity.cursor == _UINT64_MAX - 1
    assert entity.revision_cursor == _UINT64_MAX - 2
    assert MessageToDict(thread_entity_variant(entity)) == state
    assert thread_entity_state_json(entity) == state


def test_stored_protojson_may_be_a_json_string() -> None:
    record = {
        "thread_id": "session",
        "projection_epoch": "epoch",
        "entity_kind": "command",
        "entity_id": "command",
        "entity_index": "0",
        "cursor": "1",
        "revision_cursor": "1",
        "state": '{"operation":"OPERATION_INTERRUPT_TURN","outcome":"THREAD_COMMAND_OUTCOME_PENDING"}',
    }

    entity = thread_entity_from_record(record)

    assert entity.WhichOneof("entity") == "command"
    assert entity.command.operation == command_pb2.OPERATION_INTERRUPT_TURN
    assert entity.command.outcome == history_service_pb2.THREAD_COMMAND_OUTCOME_PENDING


if __name__ == "__main__":
    pytest_bazel.main()
