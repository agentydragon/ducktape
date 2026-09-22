"""Print the app's OpenAPI document to stdout, for the frontend's generated types.

The bridge's routes carry the runner protocol's messages as proto-JSON, which FastAPI documents as
bare objects; the frontend types those from `protocol.proto` itself (protobuf-es), so nothing about
them is published here.
"""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
from pydantic import TypeAdapter

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.electric import EntityInterestResponse, PayloadInterestResponse
from agentplane.app.inventory import ProvisioningState, SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.presets import Harness
from agentplane.app.trajectory import ThreadEntityView, TrajectoryStore


async def _unreachable(name: str) -> str:
    raise SandboxNotReachableError(name, ProvisioningState.WAITING_FOR_POD)


def openapi_document() -> dict[str, Any]:
    # Only routes and models shape the document; the inventory's clients are never called.
    inventory = SandboxInventory(namespace="schema", custom_objects=cast(Any, None), core_v1=cast(Any, None))
    # An engine connects lazily, so a URL nothing listens on is fine for a document.
    store = TrajectoryStore.connect("postgresql+asyncpg://schema@localhost/schema")
    document: dict[str, Any] = create_app(
        inventory,
        RunnerBridge(address_of=_unreachable, store=store),
        store,
        {harness: ["schema-model"] for harness in Harness},
        EgressInventory(namespace="schema", custom_objects=cast(Any, None)),
        DecisionsClient(httpx.AsyncClient(base_url="http://schema.invalid")),
        LiveIndex(stale_after_seconds=900),
        ActionPolicyInventory(namespace="schema", custom_objects=cast(Any, None)),
    ).openapi()
    components = document["components"]
    if not isinstance(components, dict) or not isinstance(components.get("schemas"), dict):
        raise ValueError("OpenAPI document has no schema components")
    for name, adapter in (
        ("ThreadEntityView", TypeAdapter(ThreadEntityView)),
        ("EntityInterestResponse", TypeAdapter(EntityInterestResponse)),
        ("PayloadInterestResponse", TypeAdapter(PayloadInterestResponse)),
    ):
        schema = adapter.json_schema(ref_template="#/components/schemas/{model}")
        components["schemas"].update(schema.pop("$defs", {}))
        components["schemas"][name] = schema
    return document


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
