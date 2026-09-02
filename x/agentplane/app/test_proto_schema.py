"""The protocol's JSON Schema says what proto-JSON actually emits: an event round-trips through
it, oneof members are optional, enums are names, and 64-bit sequences are strings."""

from __future__ import annotations

import jsonschema
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp

from x.agentplane.app.export_schema import openapi_document
from x.agentplane.app.proto_schema import schemas_for
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, and jsonschema's stubs' own imports, which the mypy
# aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//referencing


def _validator(root: str) -> jsonschema.Draft202012Validator:
    schemas = schemas_for(pb.Event.DESCRIPTOR, pb.Attached.DESCRIPTOR, ref_template="#/$defs/{model}")
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{root}", "$defs": schemas})


def test_an_event_as_proto_json_validates_against_the_derived_schema() -> None:
    event = pb.Event(
        sequence=12,
        at=Timestamp(seconds=1_700_000_000),
        source_sequences=[10, 11],
        item_completed=pb.ItemCompleted(item_id="toolu_1", tool=pb.ToolResult(output="done", succeeded=True)),
    )
    encoded = MessageToDict(event)
    assert encoded["sequence"] == "12"
    assert encoded["sourceSequences"] == ["10", "11"]
    _validator("Event").validate(encoded)
    _validator("Attached").validate(
        MessageToDict(
            pb.Attached(
                session_id="s", spec=pb.SessionSpec(provider=pb.PROVIDER_CLAUDE), harness=pb.HARNESS_STATE_RUNNING
            )
        )
    )


def test_the_schema_rejects_what_proto_json_never_writes() -> None:
    validator = _validator("Event")
    assert validator.is_valid({"sequence": "1", "turnCompleted": {"turnId": "t", "status": "TURN_STATUS_COMPLETED"}})
    assert not validator.is_valid({"sequence": 1})
    assert not validator.is_valid({"sequence": "1", "turnCompleted": {"status": "COMPLETED"}})
    assert not validator.is_valid({"sequence": "1", "unknownObservation": {}})


def test_the_openapi_document_publishes_the_protocol_messages_beside_the_api_models() -> None:
    schemas = openapi_document()["components"]["schemas"]
    assert {"RunnerEvent", "RunnerAttached", "RunnerSessionSummary", "RunnerSessionSpec", "RunnerInput"} <= set(schemas)
    assert {"RunnerTurnStatus", "RunnerProvider", "Provider", "SandboxView"} <= set(schemas)
    assert schemas["RunnerEvent"]["properties"]["native"] == {"$ref": "#/components/schemas/RunnerNative"}
    assert schemas["NewSession"]["properties"]["spec"] == {"$ref": "#/components/schemas/RunnerSessionSpec"}
    paths = openapi_document()["paths"]
    inputs = paths["/sandboxes/{name}/sessions/{session_id}/inputs"]["post"]["requestBody"]
    assert inputs["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/RunnerInput"}


if __name__ == "__main__":
    pytest_bazel.main()
