"""A scripted Dispatcher, for app tests that inject a Dispatcher fault or never reach one. Tests of the
runner path run the Go Dispatcher (`agentplane/app/testing/runner_gateway.py`)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2_grpc

# gazelle:include_dep @pypi//protobuf


@asynccontextmanager
async def served_dispatcher(
    service: session_dispatcher_pb2_grpc.SessionDispatcherServicer, token_file: Path, *, request_timeout_s: float
) -> AsyncIterator[SessionDispatcherClient]:
    """Serve `service` locally, and yield the app's client of it."""
    await asyncio.to_thread(token_file.write_text, "test-app-command-peer")
    server = grpc.aio.server()
    session_dispatcher_pb2_grpc.add_SessionDispatcherServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield SessionDispatcherClient(channel, token_file=token_file, request_timeout_s=request_timeout_s)
