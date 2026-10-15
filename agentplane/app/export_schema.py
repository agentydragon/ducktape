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

import grpc
import httpx
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import TypeAdapter

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.live import LiveIndex
from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.system_status import SystemStatusChecker
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.fold.views import ThreadEntityView
from agentplane.kubernetes_watch import Freshness
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient


def _openapi_document(api_client: k8s_client.ApiClient, channel: grpc.aio.Channel) -> dict[str, Any]:
    # Only routes and models shape the document; the service clients are never called.
    # Schema export never makes an RPC; the timeouts are inert placeholder arguments, not runtime defaults.
    inventory = cast(SandboxAccess, None)
    gateway = RunnerGatewayClient(
        channel,
        token_file=Path("/schema-unused-token"),
        command_admission_timeout_s=1,
        request_timeout_s=1,
        control=SandboxControlClient(
            control_target="schema.invalid:8080",
            token_file=Path("/schema-unused-token"),
            request_timeout_s=1,
            lifecycle_timeout_s=1,
        ),
    )
    dispatcher = SessionDispatcherClient(channel, token_file=Path("/schema-unused-token"), request_timeout_s=1)
    history = HistoryServiceClient("schema.invalid:8080", token_file=Path("/schema-unused-token"), request_timeout_s=1)
    # An engine connects lazily, so a URL nothing listens on is fine for a document.
    engine = connect("postgresql+asyncpg://schema@localhost/schema")
    database_updates = create_database_updates(engine.url)
    summaries = Summaries(history)
    event_logs, content = (
        EventLogStore(engine, summaries=summaries, history_reader=history),
        ContentStore(engine, history_reader=history),
    )
    live = LiveIndex(freshness=Freshness(stale_after_seconds=900), core_v1=CoreV1Api(api_client))
    action_policy = ActionPolicyInventory(namespace="schema", custom_objects=cast(Any, None))
    document: dict[str, Any] = create_app(
        inventory,
        RunnerBridge(
            runners=RunnerDirectory(live, gateway), event_logs=event_logs, content=content, dispatcher=dispatcher
        ),
        ThreadStore(engine, summaries),
        ModelCatalog(
            models=[
                ModelOption(
                    model="schema-model", display_name="Schema Model", reasoning_efforts=["low", "medium", "high"]
                )
            ],
            harnesses={harness: ["schema-model"] for harness in Harness},
        ),
        EgressAccess(namespace="schema", custom_objects=cast(Any, None), action_policy=action_policy),
        DecisionsClient(httpx.AsyncClient(base_url="http://schema.invalid")),
        live,
        action_policy,
        electric=ElectricProxy(
            httpx.AsyncClient(base_url="http://schema.invalid"),
            content,
            event_logs=event_logs,
            thread_changes=database_updates.changes[Channel.THREADS],
        ),
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=OperatorSessionStore(engine),
        notifications_http=httpx.AsyncClient(base_url="http://schema.invalid"),
        notifications_token_file=Path("/schema-unused-token"),
        system_status=SystemStatusChecker([], timeout_s=1),
    ).openapi()
    components = document["components"]
    if not isinstance(components, dict) or not isinstance(components.get("schemas"), dict):
        raise ValueError("OpenAPI document has no schema components")
    # The entity routes stream Electric's rows, so no route declares the model the frontend reads them as.
    schema = TypeAdapter(ThreadEntityView).json_schema(ref_template="#/components/schemas/{model}")
    components["schemas"].update(schema.pop("$defs", {}))
    components["schemas"]["ThreadEntityView"] = schema
    return document


async def openapi_document() -> dict[str, Any]:
    async with k8s_client.ApiClient() as api_client, dial("schema.invalid:8080") as channel:
        return _openapi_document(api_client, channel)


def main() -> None:
    print(json.dumps(asyncio.run(openapi_document()), indent=2))


if __name__ == "__main__":
    main()
