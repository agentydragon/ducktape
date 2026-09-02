"""Print the app's OpenAPI document to stdout, for the frontend's generated types.

The bridge's routes carry the runner protocol's messages as proto-JSON, which FastAPI documents as
bare objects; the frontend types those from `protocol.proto` itself (protobuf-es), so nothing about
them is published here.
"""

from __future__ import annotations

import json
from typing import Any, cast

from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from x.agentplane.app.inventory import ProvisioningState, SandboxInventory


async def _unreachable(name: str) -> str:
    raise SandboxNotReachableError(name, ProvisioningState.WAITING_FOR_POD)


def openapi_document() -> dict[str, Any]:
    # Only routes and models shape the document; the inventory's clients are never called.
    inventory = SandboxInventory(
        namespace="schema", template="schema", custom_objects=cast(Any, None), core_v1=cast(Any, None)
    )
    document: dict[str, Any] = create_app(inventory, RunnerBridge(address_of=_unreachable)).openapi()
    return document


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
