"""Thread reconnect behavior and event-driven Sandbox inventory subscriptions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict, ParseError
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from agentplane.app.testing import client as client_module
from agentplane.app.testing.client import Client, SessionStreamError
from agentplane.app.testing.models import thread_view_from_proto_json
from agentplane.env.runtime.harness import Harness as NamedHarness
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import event_log_pb2, identity_pb2, session_pb2

# A server answers one connection per call: `(after, writer)`. Raw sockets, because what is under
# test is how the client reads a connection that dies, which an ASGI transport cannot produce.
Connection = Callable[[int, asyncio.StreamWriter], Awaitable[None]]


@pytest.mark.parametrize(
    ("spec", "historical_harness", "want"),
    [
        (
            {
                "placement": {
                    "environmentHosted": {"environment": {"name": "sandbox", "uid": "uid"}, "harness": "HARNESS_CODEX"}
                }
            },
            None,
            NamedHarness.CODEX,
        ),
        (
            {"placement": {"centralSamplerV1": {"executorEnvironment": {"name": "sandbox", "uid": "uid"}}}},
            None,
            NamedHarness.CENTRAL_SAMPLER,
        ),
        ({}, "HARNESS_CLAUDE", NamedHarness.CLAUDE),
        ({}, None, None),
    ],
)
def test_thread_view_preserves_typed_or_historical_harness(
    spec: dict[str, object], historical_harness: str | None, want: NamedHarness | None
) -> None:
    attachment: dict[str, object] = {"spec": spec}
    if historical_harness is not None:
        attachment["historicalHarness"] = historical_harness
    view = thread_view_from_proto_json(
        {"threadId": str(uuid4()), "createdAt": "2026-11-04T00:00:00Z"},
        {"attached": attachment, "sandbox": {"name": "sandbox", "uid": "uid"}},
    )
    assert view.harness == want


def _frame(name: str, data: dict[str, object], *, cursor: int | None = None) -> bytes:
    ident = f"id: {cursor}\n" if cursor is not None else ""
    return f"{ident}event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def _entry(cursor: int) -> bytes:
    return _frame("event", MessageToDict(event_log_pb2.EventEntry(cursor=cursor)), cursor=cursor)


async def _respond(
    writer: asyncio.StreamWriter, status: str, body: bytes = b"", content_type: str = "text/event-stream"
) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n".encode() + body)
    await writer.drain()


@asynccontextmanager
async def _app(
    *connections: Connection, port: int = 0, requests: list[str] | None = None
) -> AsyncIterator[tuple[Client, asyncio.Server]]:
    remaining = list(connections)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        if requests is not None:
            length = next(
                (
                    int(line.split(":", 1)[1])
                    for line in request.splitlines()
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            body = (await reader.readexactly(length)).decode() if length else ""
            requests.append(request + body)
        after = int(request.split("after=")[1].split(" ")[0].split("&")[0]) if "after=" in request else 0
        try:
            await remaining.pop(0)(after, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", port)
    bound = server.sockets[0].getsockname()[1]
    async with server, Client(base_url=f"http://127.0.0.1:{bound}", token="test-token") as client:
        yield client, server


@pytest.fixture(autouse=True)
def quick_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "RECONNECT_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(client_module, "RECONNECT_SECONDS", 0.5)


async def _cursors(client: Client, after: int = 0) -> list[int]:
    return [entry.cursor async for entry in client.events(uuid4(), after=after, read_seconds=5)]


async def _first_two_then_close(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "200 OK", _entry(after + 1) + _entry(after + 2))


async def _rest_then_end(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "200 OK", _entry(after + 1) + _frame("end", {}))


async def _unavailable(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "503 Service Unavailable")


async def test_client_propagates_active_trace_context() -> None:
    observed: list[str] = []

    async def accepted(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "200 OK", b'{"presets":[]}', "application/json")

    span_context = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    with trace.use_span(NonRecordingSpan(span_context)):
        async with _app(accepted, requests=observed) as (client, _):
            await client.presets()
    assert "traceparent: 00-1234567890abcdef1234567890abcdef-1234567890abcdef-01\r\n" in observed[0].lower()


@pytest.mark.parametrize(("overrides", "setup_script"), [(None, None), ({"model": "", "instructions": None}, "")])
async def test_create_session_preserves_override_presence(
    overrides: dict[str, object] | None, setup_script: str | None
) -> None:
    observed: list[str] = []
    session_id, sandbox_uid = uuid4(), str(uuid4())

    async def opened(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "201 Created",
            json.dumps(
                {
                    "sessionId": str(session_id),
                    "status": {"materialization": "SESSION_MATERIALIZATION_WAITING"},
                    "placement": {"environmentHosted": {"environment": {"name": "demo", "uid": sandbox_uid}}},
                }
            ).encode(),
            "application/json",
        )

    async with _app(opened, requests=observed) as (client, _):
        await client.create_session(
            "demo", session_id, sandbox_uid=sandbox_uid, overrides=overrides, setup_script=setup_script
        )

    body = json.loads(observed[0].split("\r\n\r\n", 1)[1])
    assert body == {
        "session_id": str(session_id),
        "sandbox_uid": str(sandbox_uid),
        "spec": overrides or {},
        **({"setup_script": setup_script} if setup_script is not None else {}),
    }


async def test_app_config_client_decodes_generated_proto_json() -> None:
    async def models(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            json.dumps(
                {
                    "models": [{"model": "model-a", "displayName": "Model A", "reasoningEfforts": ["low"]}],
                    "harnesses": [{"harness": "HARNESS_CODEX", "models": ["model-a"]}],
                }
            ).encode(),
            "application/json",
        )

    async def presets(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            json.dumps(
                {
                    "presets": [
                        {
                            "name": "work",
                            "title": "Work",
                            "template": "linux",
                            "policySets": [],
                            "inboxTags": [],
                            "sessionDefaults": {
                                "harness": "HARNESS_CODEX",
                                "model": "model-a",
                                "cwd": "/workspace",
                                "instructions": "",
                                "setupScript": "",
                            },
                            "bootstrap": "",
                        }
                    ]
                }
            ).encode(),
            "application/json",
        )

    async with _app(models, presets) as (client, _):
        catalog = await client.models()
        presets_response = await client.presets()

    assert catalog.models[0].display_name == "Model A"
    assert catalog.harnesses == {"HARNESS_CODEX": ["model-a"]}
    (preset,) = presets_response.presets
    assert preset.session_defaults.harness == session_pb2.HARNESS_CODEX
    assert not preset.session_defaults.HasField("reasoning_effort")


async def test_runner_status_client_reads_generated_defaults_and_old_replica_names() -> None:
    async def generated(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            json.dumps(
                {
                    "environmentRuntimeBootId": "generated-boot",
                    "processes": [
                        {
                            "processId": "process-a",
                            "harness": "HARNESS_CLAUDE",
                            "state": "PROCESS_STATE_RUNNING",
                            "assignedSessionIds": ["session-a"],
                        }
                    ],
                    "sessions": [
                        {"sessionId": "session-a", "executionState": "SESSION_EXECUTION_STATE_READY", "busy": False}
                    ],
                    "lifecycle": {
                        "desiredMode": "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_QUIESCING",
                        "desiredGeneration": "7",
                        "progressGeneration": "7",
                        "progressAvailable": True,
                        "progressCurrent": True,
                        "stage": "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING",
                        "error": "",
                        "blocker": {
                            "kind": "ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT",
                            "error": "",
                            "resourceType": "IngressAcknowledgements.agentplane",
                            "resourceId": "session-a",
                            "processId": "",
                            "resourcePresent": True,
                            "admissions": "0",
                            "journalSealPresent": False,
                            "journalSealUid": "",
                            "expectedJournalSealUid": "",
                            "journalSealCursor": "9223372036854775811",
                            "acknowledgementPresent": False,
                            "acknowledgementSealed": False,
                            "acknowledgementCursor": "9223372036854775810",
                            "finalizersPending": False,
                        },
                    },
                }
            ).encode(),
            "application/json",
        )

    async def older(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"environment_runtime_boot_id":"older-boot","processes":[],"sessions":[]}',
            "application/json",
        )

    async with _app(generated, older) as (client, _):
        status = await client.runner_status("demo", "sandbox-uid")
        old_status = await client.runner_status("demo", "sandbox-uid")

    assert status.environment_runtime_boot_id == "generated-boot"
    assert status.processes[0].process_id == "process-a"
    assert status.sessions[0].execution_state == session_pb2.SESSION_EXECUTION_STATE_READY
    assert not status.sessions[0].HasField("process_id")
    assert status.lifecycle is not None
    assert status.lifecycle.blocker is not None
    assert status.lifecycle.blocker.journal_seal_cursor == 9223372036854775811
    assert old_status.environment_runtime_boot_id == "older-boot"
    assert len(old_status.processes) == 0
    assert len(old_status.sessions) == 0


async def test_resumes_after_the_last_entry_when_the_stream_closes_without_an_end_frame() -> None:
    async with _app(_first_two_then_close, _rest_then_end) as (client, _):
        assert await _cursors(client) == [1, 2, 3]


async def test_runner_status_decodes_proto_json_and_preserves_optional_and_64_bit_fields() -> None:
    async def status(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            json.dumps(
                {
                    "processes": [
                        {
                            "process_id": "process-1",
                            "pid": "9223372036854775807",
                            "harness": "HARNESS_CODEX",
                            "state": 99,
                            "assigned_session_ids": ["session-1"],
                        }
                    ],
                    "sessions": [
                        {
                            "session_id": "session-1",
                            "execution_state": "SESSION_EXECUTION_STATE_READY",
                            "failure_cursor": "18446744073709551615",
                            "busy": True,
                        }
                    ],
                    "environment_runtime_boot_id": "boot-1",
                    "lifecycle": {
                        "desired_mode": "ENVIRONMENT_RUNTIME_LIFECYCLE_MODE_RUNNING",
                        "desired_generation": "18446744073709551615",
                        "progress_generation": "9223372036854775808",
                        "progress_available": True,
                        "progress_current": True,
                        "stage": "ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_RUNNING",
                    },
                }
            ).encode(),
            "application/json",
        )

    observed: list[str] = []
    async with _app(status, requests=observed) as (client, _):
        result = await client.runner_status("test-sandbox", "sandbox-uid")

    assert "GET /sandboxes/test-sandbox/runner-status?sandbox_uid=sandbox-uid HTTP/1.1" in observed[0]
    (process,) = result.processes
    (session,) = result.sessions
    assert process.pid == 9223372036854775807
    assert process.harness == session_pb2.HARNESS_CODEX
    assert process.state == 99
    assert list(process.assigned_session_ids) == ["session-1"]
    assert "authorization: bearer test-token\r\n" in observed[0].lower()
    assert not session.HasField("process_id")
    assert not session.HasField("native_session_id")
    assert session.failure_cursor == 18446744073709551615
    assert result.lifecycle.desired_generation == 18446744073709551615
    assert result.lifecycle.progress_generation == 9223372036854775808


async def test_runner_status_rejects_unknown_proto_json_fields() -> None:
    async def status(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer, "200 OK", b'{"environment_runtime_boot_id":"boot-1","new_field":true}', "application/json"
        )

    async with _app(status) as (client, _):
        with pytest.raises(ParseError, match="new_field"):
            await client.runner_status("test-sandbox", "sandbox-uid")


async def _proxy_denied(after: int, writer: asyncio.StreamWriter) -> None:
    await _respond(writer, "403 Forbidden", b"Access denied\r\n", "text/plain")


async def test_waits_out_a_rollout_that_has_no_replica_up() -> None:
    async with _app(_first_two_then_close, _unavailable, _proxy_denied, _unavailable, _rest_then_end) as (client, _):
        assert await _cursors(client) == [1, 2, 3]


async def test_resumes_after_a_connection_reset_mid_frame() -> None:
    async def torn(after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "200 OK", _entry(after + 1) + b"event: event\ndata: {")
        writer.transport.abort()

    async with _app(torn, _rest_then_end) as (client, _):
        assert await _cursors(client) == [1, 2]


async def test_resumes_after_connection_refused_while_no_replica_listens() -> None:
    async with _app(_first_two_then_close) as (client, server):
        events = client.events(uuid4(), after=0, read_seconds=5)
        assert [(await anext(events)).cursor, (await anext(events)).cursor] == [1, 2]
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        reopened = asyncio.create_task(_serve_again(port))
        try:
            assert [entry.cursor async for entry in events] == [3]
        finally:
            (await reopened).close()


async def _serve_again(port: int) -> asyncio.Server:
    """Listen on `port` again after the client has had a connection refused."""
    await asyncio.sleep(0.1)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = (await reader.readuntil(b"\r\n\r\n")).decode()
        await _rest_then_end(int(request.split("after=")[1].split(" ")[0].split("&")[0]), writer)
        writer.close()

    return await asyncio.start_server(serve, "127.0.0.1", port)


async def test_an_outage_with_no_entries_is_reported() -> None:
    async with _app(*[_unavailable] * 100) as (client, _):
        with pytest.raises(SessionStreamError):
            await _cursors(client)


async def test_an_app_refusal_is_not_retried() -> None:
    async def refused(after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "403 Forbidden", b"{}", "application/json")

    async with _app(refused) as (client, _):
        with pytest.raises(httpx.HTTPStatusError):
            await _cursors(client)


async def test_a_client_error_is_not_retried() -> None:
    async def conflict(after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "409 Conflict")

    async with _app(conflict) as (client, _):
        with pytest.raises(Exception, match="409"):
            await _cursors(client)


async def test_sandbox_subscription_assembles_proto_snapshot_chunks() -> None:
    async def snapshots(after: int, writer: asyncio.StreamWriter) -> None:
        del after
        body = (
            b'{"result":{"snapshot":{"environments":[{"target":{"namespace":"ns","name":"one","uid":"u1"}}]}}}\n'
            b'{"result":{"snapshot":{"environments":[{"target":{"namespace":"ns","name":"two","uid":"u2"}}]}}}\n'
            b'{"result":{"snapshotComplete":{}}}\n'
        )
        await _respond(writer, "200 OK", body, "application/x-ndjson")

    async with _app(snapshots) as (client, _):
        updates = client.sandbox_snapshots()
        snapshot = await anext(updates)
        assert [(row.target.name, row.target.uid) for row in snapshot] == [("one", "u1"), ("two", "u2")]
        with pytest.raises(RuntimeError, match="inventory stream ended"):
            await anext(updates)


async def test_sandbox_subscription_applies_upserts_and_uid_fenced_removals() -> None:
    async def updates(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"result":{"snapshot":{"environments":[{"target":{"namespace":"ns","name":"box","uid":"old"}}]}}}\n'
            b'{"result":{"snapshotComplete":{}}}\n'
            b'{"result":{"upsert":{"target":{"namespace":"ns","name":"box","uid":"new"}}}}\n'
            b'{"result":{"removed":{"namespace":"ns","name":"box","uid":"old"}}}\n'
            b'{"result":{"upsert":{"target":{"namespace":"ns","name":"other","uid":"u2"}}}}\n'
            b'{"result":{"removed":{"namespace":"ns","name":"box","uid":"new"}}}\n',
            "application/x-ndjson",
        )

    async with _app(updates) as (client, _):
        stream = client.sandbox_snapshots()
        assert [(row.target.name, row.target.uid) for row in await anext(stream)] == [("box", "old")]
        assert [(row.target.name, row.target.uid) for row in await anext(stream)] == [("box", "new")]
        assert [(row.target.name, row.target.uid) for row in await anext(stream)] == [("box", "new"), ("other", "u2")]
        assert [(row.target.name, row.target.uid) for row in await anext(stream)] == [("other", "u2")]
        await stream.aclose()


async def test_sandbox_subscription_reconnects_with_a_fresh_complete_snapshot() -> None:
    async def interrupted(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"result":{"snapshot":{"environments":[{"target":{"namespace":"ns","name":"box","uid":"old"}}]}}}\n'
            b'{"result":{"snapshotComplete":{}}}\n'
            b'{"error":{"code":14,"message":"reconnect for a current snapshot"}}\n',
            "application/x-ndjson",
        )

    async def restarted(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"result":{"snapshot":{"environments":[{"target":{"namespace":"ns","name":"box","uid":"new"}}]}}}\n'
            b'{"result":{"snapshotComplete":{}}}\n',
            "application/x-ndjson",
        )

    async with _app(interrupted, restarted) as (client, _):
        stream = client.sandbox_snapshots()
        assert [(row.target.name, row.target.uid) for row in await anext(stream)] == [("box", "old")]
        assert [(row.target.name, row.target.uid) for row in await anext(stream)] == [("box", "new")]
        await stream.aclose()


async def test_sandbox_subscription_does_not_reconnect_after_grpc_denial() -> None:
    async def refused(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer, "200 OK", b'{"error":{"code":7,"message":"permission denied"}}\n', "application/x-ndjson"
        )

    async with _app(refused) as (client, _):
        with pytest.raises(RuntimeError, match="permission denied"):
            await anext(client.sandbox_snapshots())


async def test_sandbox_subscription_rejects_an_update_before_initial_snapshot() -> None:
    async def early(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"result":{"upsert":{"target":{"namespace":"ns","name":"too-early","uid":"u"}}}}\n',
            "application/x-ndjson",
        )

    async with _app(early) as (client, _):
        with pytest.raises(RuntimeError, match="before snapshot completion"):
            await anext(client.sandbox_snapshots())


@pytest.mark.parametrize("status", ["401 Unauthorized", "403 Forbidden"])
async def test_sandbox_subscription_does_not_retry_any_authorization_refusal(status: str) -> None:
    requests = 0

    async def refused(after: int, writer: asyncio.StreamWriter) -> None:
        nonlocal requests
        del after
        requests += 1
        await _respond(writer, status, b"Access denied\r\n", "text/plain")

    async with _app(refused) as (client, _):
        with pytest.raises(httpx.HTTPStatusError):
            await anext(client.sandbox_snapshots())
    assert requests == 1


async def test_get_environment_uses_the_exact_generated_route_and_uid_query() -> None:
    observed: list[str] = []

    async def descriptor(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"target":{"namespace":"test-ns","name":"box","uid":"target-uid"},'
            b'"observedState":{"operatingMode":"OPERATING_MODE_RUNNING",'
            b'"readiness":{"state":"READINESS_STATE_READY"}}}',
            "application/json",
        )

    async with _app(descriptor, requests=observed) as (client, _):
        target = identity_pb2.EnvironmentTarget(namespace="test-ns", name="box", uid="target-uid")
        environment = await client.get_environment(target)

    assert environment.target == target
    assert environment.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
    assert environment.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY
    assert "GET /upstream/environments/v1/environments/test-ns/box?target.uid=target-uid HTTP/1.1" in observed[0]


async def test_create_sandbox_captures_the_exact_complete_ack_without_retrying() -> None:
    observed: list[str] = []
    uid = "opaque-sandbox-uid"

    async def created(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "201 Created",
            json.dumps({"namespace": "test-ns", "name": "box", "uid": str(uid)}).encode(),
            "application/json",
        )

    async with _app(created, requests=observed) as (client, _):
        target = await client.create_sandbox(
            environment_service_pb2.CreateEnvironmentRequest(name="box", template="test-template")
        )

    assert target == identity_pb2.EnvironmentTarget(namespace="test-ns", name="box", uid=str(uid))
    assert client.opened_sandbox_uids == {uid}
    assert len(observed) == 1
    assert "POST /upstream/environments/v1/environments HTTP/1.1" in observed[0]


async def test_create_session_rejects_a_different_sandbox_uid() -> None:
    async def opened(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "201 Created",
            b'{"placement":{"environmentHosted":{"environment":{"name":"box","uid":"different-uid"}}}}',
            "application/json",
        )

    async with _app(opened) as (client, _):
        with pytest.raises(RuntimeError, match="different Environment identity"):
            await client.create_session("box", uuid4(), sandbox_uid="expected-uid")
        assert not client.opened_sandbox_uids


async def test_sandbox_mutations_carry_the_expected_uid() -> None:
    observed: list[str] = []
    uid = str(uuid4())

    target = identity_pb2.EnvironmentTarget(namespace="test-ns", name="box", uid=uid)

    async def accepted(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "200 OK", b"{}", "application/json")

    async def resumed(_after: int, writer: asyncio.StreamWriter) -> None:
        body = {"target": {"namespace": "test-ns", "name": "box", "uid": uid}, "outcome": "RESUME_OUTCOME_REQUESTED"}
        await _respond(writer, "200 OK", json.dumps(body).encode(), "application/json")

    async with _app(accepted, resumed, accepted, requests=observed) as (client, _):
        await client.suspend_sandbox(target)
        await client.resume_sandbox(target)
        await client.delete_sandbox(target)

    paths_and_methods = [request.split("\r\n", 1)[0] for request in observed]
    assert paths_and_methods == [
        "POST /upstream/environments/v1/environments/test-ns/box:suspend HTTP/1.1",
        "POST /upstream/environments/v1/environments/test-ns/box:resume HTTP/1.1",
        f"DELETE /upstream/environments/v1/environments/test-ns/box?target.uid={uid} HTTP/1.1",
    ]
    assert [json.loads(request.split("\r\n\r\n", 1)[1]) for request in observed[:2]] == [
        {"target": {"namespace": "test-ns", "name": "box", "uid": uid}}
    ] * 2


if __name__ == "__main__":
    pytest_bazel.main()
