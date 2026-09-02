"""Print the app's OpenAPI document to stdout, for the frontend's generated types.

The bridge's routes carry the runner protocol's messages as proto-JSON, which FastAPI documents
as bare objects; those messages are published under `components.schemas` from their descriptors so
the frontend types them from `protocol.proto` and not by hand. They carry a `Runner` prefix, since
the inventory has a `Provider` of its own (`claude`, `codex`) beside the protocol's enum.
"""

from __future__ import annotations

import json
from typing import Any, cast

from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory
from x.agentplane.app.proto_schema import schemas_for
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

# What the bridge sends and takes: the SSE stream's `attached` and `event` payloads, the session
# list, and the bodies of open and input.
PUBLISHED = (pb.Event, pb.Attached, pb.SessionSummary, pb.SessionSpec, pb.Input)
PREFIX = "Runner"


async def _unreachable(name: str) -> str:
    raise SandboxNotReachableError(name, ProvisioningState.WAITING_FOR_POD)


def openapi_document() -> dict[str, Any]:
    # Only routes and models shape the document; the inventory's clients are never called.
    inventory = SandboxInventory(
        namespace="schema", template="schema", custom_objects=cast(Any, None), core_v1=cast(Any, None)
    )
    document: dict[str, Any] = create_app(inventory, RunnerBridge(address_of=_unreachable)).openapi()
    schemas = document["components"]["schemas"]
    published = schemas_for(
        *(message.DESCRIPTOR for message in PUBLISHED), ref_template=f"#/components/schemas/{PREFIX}{{model}}"
    )
    for name, schema in published.items():
        if f"{PREFIX}{name}" in schemas:
            raise ValueError(f"the protocol message {name} collides with an API model of the same name")
        schemas[f"{PREFIX}{name}"] = schema
    _type_bridge_routes(document)
    return document


def _ref(message: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{PREFIX}{message}"}


def _type_bridge_routes(document: dict[str, Any]) -> None:
    """FastAPI documents the bridge's proto-JSON bodies as bare objects; name the messages instead."""
    paths = document["paths"]
    sessions = paths["/sandboxes/{name}/sessions"]
    sessions["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "type": "array",
        "items": _ref("SessionSummary"),
    }
    sessions["post"]["responses"]["201"]["content"]["application/json"]["schema"] = _ref("Attached")
    document["components"]["schemas"]["NewSession"]["properties"]["spec"] = _ref("SessionSpec")
    inputs = paths["/sandboxes/{name}/sessions/{session_id}/inputs"]["post"]
    inputs["requestBody"]["content"]["application/json"]["schema"] = _ref("Input")


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
