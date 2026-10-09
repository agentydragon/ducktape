"""Print the app's OpenAPI document to stdout, for the frontend's generated types.

The bridge's routes carry the runner protocol's messages as proto-JSON, which FastAPI documents as
bare objects; the frontend types those from `protocol.proto` itself (protobuf-es), so nothing about
them is published here.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import TypeAdapter

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import ModelCatalog, ModelOption, create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import DatabaseUpdates
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ThreadScopeResponse
from agentplane.app.live import LiveIndex
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.ingestion import Ingester, Ingestion
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.app.threads.view.views import ThreadEntityView
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress_views import EgressReader


def _openapi_document(api_client: k8s_client.ApiClient) -> dict[str, Any]:
    # Only routes and models shape the document; the inventory's clients are never called.
    inventory = SandboxServiceClient(
        "schema.invalid:8080",
        namespace="schema",
        token_file=Path("/schema-unused-token"),
        command_admission_timeout_s=None,
        # Schema export never makes an RPC; these are inert placeholder arguments, not runtime defaults.
        request_timeout_s=1,
        lifecycle_timeout_s=1,
        follow_timeout_s=1,
    )
    # An engine connects lazily, so a URL nothing listens on is fine for a document.
    engine = connect("postgresql+asyncpg://schema@localhost/schema")
    database_updates = DatabaseUpdates(engine.url)
    event_logs, content = EventLogStore(engine), ContentStore(engine)
    live = LiveIndex(stale_after_seconds=900, core_v1=CoreV1Api(api_client))
    runners = SandboxSessions(live, inventory)
    document: dict[str, Any] = create_app(
        inventory,
        RunnerBridge(
            runners=runners,
            event_logs=event_logs,
            content=content,
            ingester=Ingester(runners=runners, event_logs=event_logs, ingestion=Ingestion(engine)),
        ),
        ThreadStore(engine),
        ModelCatalog(
            models=[
                ModelOption(
                    model="schema-model", display_name="Schema Model", reasoning_efforts=["low", "medium", "high"]
                )
            ],
            harnesses={harness: ["schema-model"] for harness in Harness},
        ),
        EgressAccess(EgressReader(namespace="schema", custom_objects=cast(Any, None)), inventory),
        DecisionsClient(httpx.AsyncClient(base_url="http://schema.invalid")),
        live,
        ActionPolicyInventory(namespace="schema", custom_objects=cast(Any, None)),
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
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


async def openapi_document() -> dict[str, Any]:
    async with k8s_client.ApiClient() as api_client:
        return _openapi_document(api_client)


def main() -> None:
    print(json.dumps(asyncio.run(openapi_document()), indent=2))


if __name__ == "__main__":
    main()
