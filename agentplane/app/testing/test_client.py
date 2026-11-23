"""App client contracts and event-driven Sandbox inventory subscriptions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import grpc
import httpx
import pytest
import tenacity
from google.protobuf.json_format import ParseError
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from agentplane.app.testing import history_reads as history_reads_module
from agentplane.app.testing.client import Client
from agentplane.app.testing.history_reads import SessionEventReader
from agentplane.app.testing.models import thread_view_from_proto_json
from agentplane.env.runtime.harness import Harness as NamedHarness
from agentplane.environments.v1 import environment_service_pb2
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.testing.session_events import covered_cursor
from agentplane.proto import event_log_pb2, identity_pb2, session_pb2
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient

# A server answers one connection per call: `(after_cursor, writer)`. Raw sockets let the tests
# observe the exact generated-facade request and response.
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


@pytest.mark.parametrize("has_metadata", [False, True])
async def test_thread_projection_uses_dispatcher_membership_with_optional_metadata(has_metadata: bool) -> None:
    session_id = uuid4()

    def respond(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case path if path == f"/upstream/dispatcher/v1/sessions/{session_id}":
                return httpx.Response(
                    200, json={"session": {"sessionId": str(session_id), "createdAt": "2026-11-01T00:00:00Z"}}
                )
            case path if path == f"/upstream/history/v1/sessions/{session_id}/summary":
                return httpx.Response(200, json={"sessionId": str(session_id), "foldedThrough": "7"})
            case path if path == f"/upstream/app/v1/threads/{session_id}":
                return httpx.Response(
                    200 if has_metadata else 404,
                    json={
                        "threadId": str(session_id),
                        "createdAt": "2026-11-02T00:00:00Z",
                        "name": "Test title",
                        "archived": True,
                    }
                    if has_metadata
                    else {},
                )
            case _:
                raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(base_url="https://app.test", transport=httpx.MockTransport(respond)) as http:
        view = await Client(http).wait_for_thread_projection(session_id, timeout_s=1)
    assert view.id == session_id
    assert view.created_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert view.last_cursor == 7
    assert view.name == ("Test title" if has_metadata else None)
    assert view.archived is has_metadata


async def test_thread_projection_propagates_metadata_authorization_failure() -> None:
    session_id = uuid4()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/upstream/dispatcher/v1/sessions/{session_id}":
            return httpx.Response(200, json={"session": {"sessionId": str(session_id)}})
        if request.url.path == f"/upstream/history/v1/sessions/{session_id}/summary":
            return httpx.Response(200, json={})
        return httpx.Response(403, json={"message": "denied"})

    async with httpx.AsyncClient(base_url="https://app.test", transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await Client(http).thread(session_id)
    assert caught.value.response.status_code == 403


async def test_artifact_inventory_pages_dispatcher_sessions_without_app_metadata() -> None:
    first, second, pending, other = (str(uuid4()) for _ in range(4))
    observed: list[httpx.Request] = []

    def session(session_id: str, uid: str, *, central: bool = False) -> dict[str, object]:
        placement = (
            {"centralSamplerV1": {"executorEnvironment": {"name": "test-box", "uid": uid}}}
            if central
            else {"environmentHosted": {"environment": {"name": "test-box", "uid": uid}}}
        )
        return {"sessionId": session_id, "createdAt": "2026-11-01T00:00:00Z", "placement": placement}

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path == "/upstream/dispatcher/v1/sessions":
            assert request.url.params["limit"] == "100"
            if request.url.params.get("page_token") == "next":
                return httpx.Response(200, json={"sessions": [session(second, "test-uid", central=True)]})
            return httpx.Response(
                200,
                json={
                    "sessions": [session(first, "test-uid"), session(pending, "test-uid"), session(other, "other-uid")],
                    "nextPageToken": "next",
                },
            )
        if request.url.path in {
            f"/upstream/history/v1/sessions/{first}/summary",
            f"/upstream/history/v1/sessions/{second}/summary",
        }:
            return httpx.Response(200, json={})
        if request.url.path in {
            f"/upstream/app/v1/threads/{first}",
            f"/upstream/app/v1/threads/{second}",
            f"/upstream/history/v1/sessions/{pending}/summary",
        }:
            return httpx.Response(404, json={})
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(base_url="https://app.test", transport=httpx.MockTransport(respond)) as http:
        views = [view async for view in Client(http).threads(sandbox_uid="test-uid")]
    assert [str(view.id) for view in views] == [first, second]
    assert not any(request.url.path == "/upstream/app/v1/threads" for request in observed)


async def test_native_dispatcher_transport_avoids_operator_only_http_facade() -> None:
    session_id = uuid4()
    session = session_dispatcher_pb2.Session(
        session_id=str(session_id),
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name="test-box", uid="test-uid")
            )
        ),
    )
    session.created_at.FromJsonString("2026-11-01T00:00:00Z")
    dispatcher = create_autospec(SessionDispatcherClient, instance=True)
    dispatcher.stub = SimpleNamespace(ListSessions=object())
    dispatcher.get_session.return_value = session
    dispatcher.unary.return_value = session_dispatcher_pb2.ListSessionsResponse(sessions=[session])

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/upstream/history/v1/sessions/{session_id}/summary":
            return httpx.Response(200, json={})
        if request.url.path == f"/upstream/app/v1/threads/{session_id}":
            return httpx.Response(404, json={})
        raise AssertionError(f"native client must not use the operator HTTP facade: {request.url}")

    async with httpx.AsyncClient(base_url="https://app.test", transport=httpx.MockTransport(respond)) as http:
        client = Client(http, dispatcher=dispatcher)
        assert (await client.thread(session_id)).id == session_id
        assert [view.id async for view in client.threads(sandbox_uid="test-uid")] == [session_id]
    dispatcher.get_session.assert_awaited_once_with(str(session_id))
    dispatcher.unary.assert_awaited_once_with(
        dispatcher.stub.ListSessions, session_dispatcher_pb2.ListSessionsRequest(limit=100)
    )


async def _respond(
    writer: asyncio.StreamWriter, status: str, body: bytes = b"", content_type: str = "application/json"
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
        request_target = request.splitlines()[0].split()[1]
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
        after = int(parse_qs(urlsplit(request_target).query).get("after_cursor", ["0"])[0])
        try:
            await remaining.pop(0)(after, writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", port)
    bound = server.sockets[0].getsockname()[1]
    async with server, Client.connect(base_url=f"http://127.0.0.1:{bound}", token="test-token") as client:
        yield client, server


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


async def test_client_uses_the_callers_authentication_and_lifetime() -> None:
    observed: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"presets": []})

    async with httpx.AsyncClient(
        base_url="https://app.test", cookies={"operator": "cookie-token"}, transport=httpx.MockTransport(respond)
    ) as http:
        client = Client(http)
        await client.presets()
        assert observed[0].headers.get("cookie") == "operator=cookie-token"
        assert "authorization" not in observed[0].headers
        assert not http.is_closed


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


async def test_runtime_diagnostics_decodes_proto_json_and_preserves_optional_and_64_bit_fields() -> None:
    async def status(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            json.dumps(
                {
                    "target": {"namespace": "test-envs", "name": "test-sandbox", "uid": "sandbox-uid"},
                    "incarnationUid": "fixture-incarnation",
                    "observedAt": "2026-01-01T00:00:00Z",
                    "status": {
                        "processes": [
                            {
                                "process_id": "process-1",
                                "pid": "9223372036854775807",
                                "harness": "HARNESS_CODEX",
                                "state": 99,
                                "assigned_session_ids": ["session-1"],
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
                    },
                }
            ).encode(),
            "application/json",
        )

    observed: list[str] = []
    async with _app(status, requests=observed) as (client, _):
        result = await client.runtime_diagnostics(
            identity_pb2.EnvironmentTarget(namespace="test-envs", name="test-sandbox", uid="sandbox-uid")
        )

    assert (
        "GET /upstream/environments/v1/environments/test-envs/test-sandbox:runtime-diagnostics?target.uid=sandbox-uid HTTP/1.1"
        in observed[0]
    )
    (process,) = result.processes
    assert process.pid == 9223372036854775807
    assert process.harness == session_pb2.HARNESS_CODEX
    assert process.state == 99
    assert list(process.assigned_session_ids) == ["session-1"]
    assert "authorization: bearer test-token\r\n" in observed[0].lower()
    assert result.lifecycle.desired_generation == 18446744073709551615
    assert result.lifecycle.progress_generation == 9223372036854775808


async def test_runtime_diagnostics_rejects_unknown_proto_json_fields() -> None:
    async def status(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            b'{"status":{"environment_runtime_boot_id":"boot-1","new_field":true}}',
            "application/json",
        )

    async with _app(status) as (client, _):
        with pytest.raises(ParseError, match="new_field"):
            await client.runtime_diagnostics(
                identity_pb2.EnvironmentTarget(namespace="test-envs", name="test-sandbox", uid="sandbox-uid")
            )


@pytest.mark.parametrize(
    "invalid",
    [
        {"target": {"namespace": "other", "name": "test-sandbox", "uid": "sandbox-uid"}},
        {"target": {"namespace": "test-envs", "name": "other", "uid": "sandbox-uid"}},
        {"target": {"namespace": "test-envs", "name": "test-sandbox", "uid": "replacement-uid"}},
        {"target": None},
        {"incarnationUid": ""},
        {"observedAt": None},
        {"status": {}},
    ],
    ids=["namespace", "name", "uid", "missing-target", "missing-incarnation", "missing-observation", "missing-boot"],
)
async def test_runtime_diagnostics_refuses_mismatched_or_incomplete_evidence(invalid: dict[str, object]) -> None:
    async def status(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer,
            "200 OK",
            json.dumps(
                {
                    "target": {"namespace": "test-envs", "name": "test-sandbox", "uid": "sandbox-uid"},
                    "incarnationUid": "fixture-incarnation",
                    "observedAt": "2026-01-01T00:00:00Z",
                    "status": {"environmentRuntimeBootId": "boot-1"},
                    **invalid,
                }
            ).encode(),
            "application/json",
        )

    async with _app(status) as (client, _):
        with pytest.raises(RuntimeError, match="Runtime evidence"):
            await client.runtime_diagnostics(
                identity_pb2.EnvironmentTarget(namespace="test-envs", name="test-sandbox", uid="sandbox-uid")
            )


def _entry(cursor: int) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(cursor=cursor)


def _unavailable_error(status: int = 503) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://app.test.invalid/history")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"History returned {status}", request=request, response=response)


def test_covered_cursor_does_not_skip_the_tail_of_a_full_page() -> None:
    page = history_service_pb2.ReadSessionEventsResponse(last_cursor=100)
    page.entries.add(cursor=1)
    page.entries.add(cursor=5)
    page.settlements.add().ranges.add(first=2, last=4)

    assert covered_cursor(0, 2, page) == 5


def test_covered_cursor_accepts_a_large_removed_range_without_enumerating_it() -> None:
    page = history_service_pb2.ReadSessionEventsResponse(last_cursor=2**63)
    page.removed.add(first=1, last=2**63)

    assert covered_cursor(0, 1, page) == 2**63


def test_covered_cursor_rejects_an_unreported_gap() -> None:
    page = history_service_pb2.ReadSessionEventsResponse(last_cursor=4)
    page.entries.add(cursor=1)
    page.entries.add(cursor=4)

    with pytest.raises(ValueError, match="cursor 2"):
        covered_cursor(0, 2, page)


async def test_client_reads_generated_history_pages_with_the_exact_facade_route() -> None:
    observed: list[str] = []
    thread_id = uuid4()

    async def history(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(
            writer, "200 OK", b'{"lastCursor":"18446744073709551615","entries":[{"cursor":"9223372036854775808"}]}'
        )

    async with _app(history, requests=observed) as (client, _):
        page = await client.read_session_events(thread_id, after_cursor=9223372036854775807, limit=1)

    assert page.last_cursor == 18446744073709551615
    assert page.entries[0].cursor == 9223372036854775808
    request = observed[0].split("\r\n", 1)[0]
    assert request == (
        f"GET /upstream/history/v1/sessions/{thread_id}/events?after_cursor=9223372036854775807&limit=1 HTTP/1.1"
    )
    assert "authorization: bearer test-token\r\n" in observed[0].lower()


async def test_client_reads_raw_summary_cursors_without_collapsing_folded_and_committed() -> None:
    thread_id = uuid4()

    async def summary(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "200 OK", b'{"committedCursor":"22","foldedThrough":"19"}')

    async with _app(summary) as (client, _):
        result = await client.session_summary(thread_id)

    assert result.committed_cursor == 22
    assert result.folded_through == 19


async def test_history_reader_applies_settlement_and_removal_corrections() -> None:
    client = create_autospec(Client, instance=True)
    first = history_service_pb2.ReadSessionEventsResponse(last_cursor=2)
    first.entries.extend([_entry(1), _entry(2)])
    second = history_service_pb2.ReadSessionEventsResponse(last_cursor=3)
    second.entries.append(_entry(3))
    second.settlements.add(item_id="item", completion_cursor=3).ranges.add(first=1, last=1)
    second.removed.add(first=2, last=2)
    client.read_session_events.side_effect = [first, second]
    reader = SessionEventReader(client, uuid4(), limit=2)

    assert [entry.cursor for entry in await reader.read_page()] == [1, 2]
    assert [entry.cursor for entry in await reader.read_page()] == [3]
    assert [entry.cursor for entry in reader.entries] == [3]


async def test_history_reader_rejects_page_without_mutating_reader_state() -> None:
    client = create_autospec(Client, instance=True)
    first = history_service_pb2.ReadSessionEventsResponse(last_cursor=4)
    first.entries.extend([_entry(1), _entry(2)])
    invalid = history_service_pb2.ReadSessionEventsResponse(last_cursor=4)
    invalid.entries.append(_entry(4))
    invalid.removed.add(first=1, last=1)
    client.read_session_events.side_effect = [first, invalid]
    reader = SessionEventReader(client, uuid4(), limit=2)

    assert [entry.cursor for entry in await reader.read_page()] == [1, 2]
    assert reader.cursor == 2
    assert reader.more_available

    with pytest.raises(ValueError, match="did not account for cursor 3"):
        await reader.read_page()

    assert [entry.cursor for entry in reader.entries] == [1, 2]
    assert reader.cursor == 2
    assert reader.more_available


async def test_history_reader_returns_one_page_so_callers_can_evaluate_predicates() -> None:
    client = create_autospec(Client, instance=True)
    first = history_service_pb2.ReadSessionEventsResponse(last_cursor=3)
    first.entries.extend([_entry(1), _entry(2)])
    second = history_service_pb2.ReadSessionEventsResponse(last_cursor=10)
    second.entries.extend([_entry(3), _entry(4)])
    client.read_session_events.side_effect = [first, second]
    reader = SessionEventReader(client, uuid4(), limit=2)

    assert [entry.cursor for entry in await reader.read_page()] == [1, 2]
    # A full page ending at cursor 2 must not jump to the first response's global watermark 3.
    assert reader.cursor == 2
    assert reader.more_available
    assert [entry.cursor for entry in await reader.read_page()] == [3, 4]
    assert reader.cursor == 4
    assert client.read_session_events.await_count == 2


@pytest.mark.parametrize("healthy_kind", ["empty_page", "pending_attachment"])
async def test_history_reader_resets_outage_budget_after_a_healthy_response(
    healthy_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(history_reads_module, "RECONNECT_PAUSE_SECONDS", 0)
    monkeypatch.setattr(history_reads_module, "RECONNECT_SECONDS", 10)
    elapsed = [0.0]
    monkeypatch.setattr(tenacity, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    client = create_autospec(Client, instance=True)
    reader = SessionEventReader(client, uuid4())
    healthy = history_service_pb2.ReadSessionEventsResponse()
    if healthy_kind == "empty_page":
        responses: list[BaseException | history_service_pb2.ReadSessionEventsResponse] = [
            _unavailable_error(),
            healthy,
            _unavailable_error(),
            healthy,
        ]
        advances = [9.0, 100.0, 0.5, 0.0]
    else:
        missing_request = httpx.Request("GET", "https://app.test.invalid/history")
        missing = httpx.HTTPStatusError(
            "pending", request=missing_request, response=httpx.Response(404, request=missing_request)
        )
        responses = [_unavailable_error(), missing, _unavailable_error(), healthy]
        advances = [9.0, 0.0, 0.5, 0.0]

        async def session(_thread_id: object) -> object:
            elapsed[0] += 100
            return object()

        client.session.side_effect = session

    async def read(_thread_id: object, *, after_cursor: int, limit: int) -> object:
        del after_cursor, limit
        elapsed[0] += advances.pop(0)
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    client.read_session_events.side_effect = read

    assert await reader.read_page() == []
    if healthy_kind == "pending_attachment":
        assert reader.pending_attachment
    # The healthy response ends the first outage window. A later brief outage gets a fresh budget.
    assert await reader.read_page() == []
    assert client.read_session_events.await_count == 4


async def test_history_reader_retries_proxy_outage_but_not_real_authorization_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_reads_module, "RECONNECT_PAUSE_SECONDS", 0)
    requests: list[str] = []

    async def unavailable(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "503 Service Unavailable")

    async def proxy_denied(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "403 Forbidden", b"Access denied\r\n", "text/plain")

    async def healthy(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "200 OK", b'{"lastCursor":"0"}')

    async with _app(unavailable, proxy_denied, healthy, requests=requests) as (client, _):
        assert await SessionEventReader(client, uuid4()).read_page() == []
    assert len(requests) == 3

    async def app_refused(_after: int, writer: asyncio.StreamWriter) -> None:
        await _respond(writer, "403 Forbidden", b"{}", "application/json")

    async with _app(app_refused, requests=requests) as (client, _):
        with pytest.raises(httpx.HTTPStatusError):
            await SessionEventReader(client, uuid4()).read_page()
    assert len(requests) == 4


async def test_history_reader_bounds_a_prolonged_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history_reads_module, "RECONNECT_SECONDS", 10)
    monkeypatch.setattr(history_reads_module, "RECONNECT_PAUSE_SECONDS", 0)
    elapsed = [0.0]
    monkeypatch.setattr(tenacity, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    client = create_autospec(Client, instance=True)

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        elapsed[0] += 6
        raise _unavailable_error()

    client.read_session_events.side_effect = unavailable

    with pytest.raises(TimeoutError, match="stayed unavailable"):
        await SessionEventReader(client, uuid4()).read_page()
    assert client.read_session_events.await_count == 2


async def test_history_reader_treats_only_initial_history_404_as_pending_attachment() -> None:
    thread_id = uuid4()
    client = create_autospec(Client, instance=True)
    client.read_session_events.return_value = history_service_pb2.ReadSessionEventsResponse()
    reader = SessionEventReader(client, thread_id)
    await reader.read_page()
    missing_request = httpx.Request("GET", f"https://app.test.invalid/sessions/{thread_id}/events")
    missing = httpx.HTTPStatusError(
        "missing", request=missing_request, response=httpx.Response(404, request=missing_request)
    )
    client.read_session_events.side_effect = missing

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await reader.read_page()

    assert caught.value is missing
    client.session.assert_not_awaited()


async def test_history_reader_waits_for_history_attachment_when_dispatcher_session_exists() -> None:
    client = create_autospec(Client, instance=True)
    request = httpx.Request("GET", "https://app.test.invalid/history")
    missing = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
    client.read_session_events.side_effect = missing
    client.session.return_value = object()
    reader = SessionEventReader(client, uuid4())

    assert await reader.read_page() == []
    assert reader.pending_attachment
    assert client.session.await_count == 1


async def test_history_reader_propagates_unknown_initial_session() -> None:
    client = create_autospec(Client, instance=True)
    request = httpx.Request("GET", "https://app.test.invalid/history")
    missing = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
    client.read_session_events.side_effect = missing
    client.session.side_effect = missing

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await SessionEventReader(client, uuid4()).read_page()

    assert caught.value is missing


@pytest.mark.parametrize("code", [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED])
async def test_history_reader_preserves_native_dispatcher_refusals(code: grpc.StatusCode) -> None:
    client = create_autospec(Client, instance=True)
    request = httpx.Request("GET", "https://app.test.invalid/history")
    missing = httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))
    refusal = ServiceError(code, "test refusal")
    client.read_session_events.side_effect = missing
    client.session.side_effect = refusal

    with pytest.raises((httpx.HTTPStatusError, ServiceError)) as caught:
        await SessionEventReader(client, uuid4()).read_page()
    assert caught.value is (missing if code == grpc.StatusCode.NOT_FOUND else refusal)


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
        body = {
            "target": {"namespace": "test-ns", "name": "box", "uid": uid},
            "outcome": "RESUME_OUTCOME_REQUESTED",
            "wakeGeneration": "8",
        }
        await _respond(writer, "200 OK", json.dumps(body).encode(), "application/json")

    async with _app(accepted, resumed, accepted, requests=observed) as (client, _):
        await client.suspend_sandbox(target)
        response = await client.resume_sandbox(target)
        await client.delete_sandbox(target)

    assert response.target == target
    assert response.outcome == environment_service_pb2.RESUME_OUTCOME_REQUESTED
    assert response.HasField("wake_generation")
    assert response.wake_generation == 8

    paths_and_methods = [request.split("\r\n", 1)[0] for request in observed]
    assert paths_and_methods == [
        "POST /upstream/environments/v1/environments/test-ns/box:suspend HTTP/1.1",
        "POST /upstream/environments/v1/environments/test-ns/box:resume HTTP/1.1",
        f"DELETE /upstream/environments/v1/environments/test-ns/box?target.uid={uid} HTTP/1.1",
    ]
    assert [json.loads(request.split("\r\n\r\n", 1)[1]) for request in observed[:2]] == [
        {"target": {"namespace": "test-ns", "name": "box", "uid": uid}}
    ] * 2
