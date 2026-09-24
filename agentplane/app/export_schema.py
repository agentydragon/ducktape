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
from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.ingestion import Ingester, Ingestion
from agentplane.app.agent_runtime.runner.bridge import RunnerBridge
from agentplane.app.agent_runtime.runner.runners import Runners
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.agent_runtime.updates import ThreadUpdates
from agentplane.app.agent_runtime.view.content import ContentStore
from agentplane.app.agent_runtime.view.views import ThreadEntityView
from agentplane.app.api import create_app
from agentplane.app.database import connect
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.electric import ThreadScopeResponse
from agentplane.app.inventory import SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import Harness


def openapi_document() -> dict[str, Any]:
    # Only routes and models shape the document; the inventory's clients are never called.
    inventory = SandboxInventory(namespace="schema", custom_objects=cast(Any, None), core_v1=cast(Any, None))
    # An engine connects lazily, so a URL nothing listens on is fine for a document.
    engine = connect("postgresql+asyncpg://schema@localhost/schema")
    thread_updates = ThreadUpdates(engine.url)
    event_logs, content = EventLogStore(engine), ContentStore(engine)
    live = LiveIndex(stale_after_seconds=900)
    runners = Runners(live, port=1)
    document: dict[str, Any] = create_app(
        inventory,
        RunnerBridge(
            runners=runners,
            event_logs=event_logs,
            content=content,
            ingester=Ingester(runners=runners, event_logs=event_logs, ingestion=Ingestion(engine)),
            thread_changes=thread_updates.changes,
        ),
        ThreadStore(engine),
        {harness: ["schema-model"] for harness in Harness},
        EgressInventory(namespace="schema", custom_objects=cast(Any, None)),
        DecisionsClient(httpx.AsyncClient(base_url="http://schema.invalid")),
        live,
        ActionPolicyInventory(namespace="schema", custom_objects=cast(Any, None)),
        event_logs=event_logs,
        content=content,
        thread_updates=thread_updates,
        operator_sessions=OperatorSessionStore(engine),
    ).openapi()
    components = document["components"]
    if not isinstance(components, dict) or not isinstance(components.get("schemas"), dict):
        raise ValueError("OpenAPI document has no schema components")
    for name, adapter in (
        ("ThreadEntityView", TypeAdapter(ThreadEntityView)),
        ("ThreadScopeResponse", TypeAdapter(ThreadScopeResponse)),
    ):
        schema = adapter.json_schema(ref_template="#/components/schemas/{model}")
        components["schemas"].update(schema.pop("$defs", {}))
        components["schemas"][name] = schema
    return document


def main() -> None:
    print(json.dumps(openapi_document(), indent=2))


if __name__ == "__main__":
    main()
