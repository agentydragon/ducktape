"""Failure and interrupt settlement preserve safe subsequent turns."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
from aiohttp import ClientSession, WSMsgType, web

from agentplane.native.codex.testing.responses_sse import CustomToolCall, FunctionCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.central_sampler.testing_store import Store
from util.net import listen_tcp
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with postgres_async_engine(database_url) as engine:
        yield Store(engine)


async def wait_turn(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, turn_id: str) -> event_pb2.TurnCompleted:
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(15):
            async for frame in stream:
                if frame.entry.event.HasField("turn_completed") and frame.entry.event.turn_completed.turn_id == turn_id:
                    return frame.entry.event.turn_completed
    finally:
        stream.cancel()
    raise AssertionError("event stream ended before turn completion")


@dataclass
class ExecutorProxy:
    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    initializations: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    dropped: asyncio.Event = field(default_factory=asyncio.Event)
    effect_observed: asyncio.Event = field(default_factory=asyncio.Event)
    dropped_request: dict[str, Any] | None = None
    dropped_reply: dict[str, Any] | None = None


@asynccontextmanager
async def faulting_executor_proxy(executor_url: str, *, drop_method: str, marker: str) -> AsyncIterator[ExecutorProxy]:
    """Forward to the real exec-server and drop one selected RPC reply."""
    observed = ExecutorProxy(url="")

    def is_target_start(request: dict[str, Any]) -> bool:
        params = request.get("params", {})
        return request.get("method") == "process/start" and marker in json.dumps(params.get("argv", []))

    def is_target_read(request: dict[str, Any]) -> bool:
        if request.get("method") != "process/read":
            return False
        process_id = request.get("params", {}).get("processId")
        return any(
            is_target_start(start) and start.get("params", {}).get("processId") == process_id
            for start in observed.requests
        )

    def is_target(request: dict[str, Any]) -> bool:
        if drop_method == "process/start":
            return is_target_start(request)
        if drop_method == "process/read":
            return is_target_read(request)
        raise AssertionError(f"unsupported fault injection method: {drop_method}")

    async def proxy(request: web.Request) -> web.WebSocketResponse:
        downstream = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
        await downstream.prepare(request)
        async with ClientSession() as http, http.ws_connect(executor_url, max_msg_size=16 * 1024 * 1024) as upstream:
            requests_by_id: dict[str, dict[str, Any]] = {}
            pending_start_reply: tuple[dict[str, Any], dict[str, Any]] | None = None
            effect_observed = False

            async def drop_connection(target_request: dict[str, Any], target_reply: dict[str, Any]) -> None:
                observed.dropped_request = target_request
                observed.dropped_reply = target_reply
                observed.dropped.set()
                await upstream.close()
                await downstream.close()

            async def forward_requests() -> None:
                async for message in downstream:
                    if message.type is WSMsgType.TEXT:
                        payload = json.loads(message.data)
                        observed.requests.append(payload)
                        if "id" in payload:
                            requests_by_id[str(payload["id"])] = payload
                        await upstream.send_str(message.data)
                    elif message.type is WSMsgType.BINARY:
                        await upstream.send_bytes(message.data)
                    else:
                        return

            async def forward_replies() -> None:
                nonlocal effect_observed, pending_start_reply
                async for message in upstream:
                    if message.type is WSMsgType.TEXT:
                        payload = json.loads(message.data)
                        observed.replies.append(payload)
                        request_id = payload.get("id")
                        original = requests_by_id.get(str(request_id)) if request_id is not None else None
                        if original is not None and original.get("method") == "initialize":
                            observed.initializations.append((original, payload))

                        if payload.get("method") == "process/output":
                            params = payload.get("params", {})
                            target_process_ids = {
                                start.get("params", {}).get("processId")
                                for start in observed.requests
                                if is_target_start(start)
                            }
                            if params.get("processId") in target_process_ids and marker.encode() in base64.b64decode(
                                params["chunk"]
                            ):
                                effect_observed = True
                                observed.effect_observed.set()
                                if drop_method == "process/start" and pending_start_reply is not None:
                                    await drop_connection(*pending_start_reply)
                                    return

                        if original is not None and is_target(original) and not observed.dropped.is_set():
                            if drop_method == "process/read":
                                chunks = payload.get("result", {}).get("chunks", [])
                                output = b"".join(base64.b64decode(chunk["chunk"]) for chunk in chunks)
                                if marker.encode() in output:
                                    effect_observed = True
                                    observed.effect_observed.set()
                                    await drop_connection(original, payload)
                                    return
                                await downstream.send_str(message.data)
                                continue
                            if effect_observed:
                                await drop_connection(original, payload)
                                return
                            pending_start_reply = (original, payload)
                            continue
                        await downstream.send_str(message.data)
                    elif message.type is WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)
                    else:
                        return

            tasks = [asyncio.create_task(forward_requests()), asyncio.create_task(forward_replies())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return downstream

    app = web.Application()
    app.router.add_get("/", proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    with listen_tcp() as listener:
        try:
            await web.SockSite(runner, listener.socket).start()
            observed.url = f"ws://127.0.0.1:{listener.port}"
            yield observed
        finally:
            await runner.cleanup()


@pytest.mark.parametrize("failure", ["failed", "429", "503", "unsupported", "invalid_wait", "partial"])
async def test_failed_turn_preserves_safe_continuation(
    store: Store,
    database_url: str,
    executor_url: str,
    codex_binary: str,
    code_mode_host: str,
    tmp_path: Path,
    failure: str,
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(session_id=request.session_id, command_id="first", text="Start"),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                if failure in {"429", "503"}:
                    await exchange.respond(JsonResponse(b'{"error":{"code":"rate_limit_exceeded"}}', int(failure)))
                elif failure == "failed":
                    payload = {
                        "type": "response.failed",
                        "response": {"status": "failed", "error": {"code": "rate_limit_exceeded"}},
                    }
                    await exchange.send(SseEvent("response.failed", ("data: " + json.dumps(payload) + "\n\n").encode()))
                elif failure == "unsupported":
                    await exchange.send(
                        *response_stream(
                            [CustomToolCall("unsupported", "invalid", "text(1)")], model="test-model"
                        ).events
                    )
                elif failure == "invalid_wait":
                    await exchange.send(
                        *response_stream(
                            [
                                FunctionCall("bad-wait", "wait", {"cell_id": "missing"}),
                                CustomToolCall(
                                    "sibling", "exec", 'text(await tools.exec_command({cmd:"touch unwanted"}));'
                                ),
                            ],
                            model="test-model",
                        ).events
                    )
                else:
                    events = response_stream([Message("Partial")], model="test-model").through(
                        "response.output_text.delta"
                    )
                    await exchange.send(*events.events)
            assert (await wait_turn(stub, request.session_id, "first")).status == event_pb2.TURN_STATUS_FAILED
            observed = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
            )
            assert observed.blocked == (failure == "partial")
            command = await stub.GetCommand(
                sampler_pb2.GetCommandRequest(session_id=request.session_id, command_id="first"), metadata=METADATA
            )
            assert command.outcome == (
                sampler_pb2.COMMAND_OUTCOME_UNKNOWN if failure == "partial" else sampler_pb2.COMMAND_OUTCOME_FAILED
            )
            intents = await store.intents()
            if failure == "partial":
                assert any(intent.result is None for intent in intents)
            else:
                rejected = [
                    intent
                    for intent in intents
                    if intent.result is not None and intent.result.get("status") == "rejected"
                ]
                assert len(rejected) == 1
                if failure == "failed":
                    assert rejected[0].result is not None
                    assert rejected[0].result["terminal"]["response"]["error"]["code"] == "rate_limit_exceeded"
                if failure in {"429", "503"}:
                    assert rejected[0].result is not None
                    assert rejected[0].result["http_status"] == int(failure)
            if failure == "partial":
                return
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="second", text="Continue"
                ),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                if failure == "invalid_wait":
                    outputs = [
                        item
                        for item in exchange.request.body["input"]
                        if item["type"] in {"custom_tool_call_output", "function_call_output"}
                    ]
                    assert {item["call_id"] for item in outputs} == {"bad-wait", "sibling"}
                    assert not (tmp_path / "unwanted").exists()
                await exchange.send(*response_stream([Message("Recovered")], model="test-model").events)
            assert (await wait_turn(stub, request.session_id, "second")).status == event_pb2.TURN_STATUS_COMPLETED


async def test_lost_process_read_reply_resumes_same_process_and_cursor(
    store: Store, database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    output = "RECOVERED_PROCESS_OUTPUT"
    async with (
        Endpoint() as endpoint,
        faulting_executor_proxy(executor_url, drop_method="process/read", marker=output) as proxy,
    ):
        config = settings(
            database_url, endpoint.origin, proxy.url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="read-recovery", text="Run one command."
                ),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                source = 'text(await tools.exec_command({cmd: "printf x >> fault-marker && printf ' + output + '"}));'
                await exchange.send(
                    *response_stream([CustomToolCall("read-recovery", "exec", source)], model="test-model").events
                )
            async with await endpoint.next() as exchange:
                assert output in json.dumps(exchange.request.body["input"])
                await exchange.send(*response_stream([Message("completed after reconnect")], model="test-model").events)
            assert (
                await wait_turn(stub, request.session_id, "read-recovery")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
            )
            assert not session.blocked

    assert proxy.dropped.is_set()
    assert proxy.effect_observed.is_set()
    assert (tmp_path / "fault-marker").read_text() == "x"
    starts = [
        call
        for call in proxy.requests
        if call.get("method") == "process/start" and "fault-marker" in json.dumps(call.get("params", {}).get("argv"))
    ]
    assert len(starts) == 1
    process_id = starts[0]["params"]["processId"]
    reads = [
        call
        for call in proxy.requests
        if call.get("method") == "process/read" and call.get("params", {}).get("processId") == process_id
    ]
    assert proxy.dropped_request is not None
    retry = [call for call in reads if call == proxy.dropped_request]
    assert len(retry) == 2
    first_read = reads.index(proxy.dropped_request)
    assert reads[first_read + 1] == proxy.dropped_request
    assert proxy.dropped_reply is not None
    assert "error" not in proxy.dropped_reply
    dropped_chunks = proxy.dropped_reply["result"]["chunks"]
    assert output.encode() in b"".join(base64.b64decode(chunk["chunk"]) for chunk in dropped_chunks)
    assert len(proxy.initializations) == 2
    executor_session_id = proxy.initializations[0][1]["result"]["sessionId"]
    assert proxy.initializations[0][0]["params"]["resumeSessionId"] is None
    assert proxy.initializations[1][0]["params"]["resumeSessionId"] == executor_session_id
    assert proxy.initializations[1][1]["result"]["sessionId"] == executor_session_id
    assert all(intent.result is not None for intent in await store.intents())


async def test_lost_process_start_ack_is_not_replayed_and_blocks_session(
    store: Store, database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    output = "START_EFFECT_OBSERVED"
    async with (
        Endpoint() as endpoint,
        faulting_executor_proxy(executor_url, drop_method="process/start", marker=output) as proxy,
    ):
        config = settings(
            database_url, endpoint.origin, proxy.url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="start-ack-lost", text="Run one command."
                ),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                source = 'text(await tools.exec_command({cmd: "printf x >> fault-marker && printf ' + output + '"}));'
                await exchange.send(
                    *response_stream([CustomToolCall("start-ack-lost", "exec", source)], model="test-model").events
                )
            assert (await wait_turn(stub, request.session_id, "start-ack-lost")).status == event_pb2.TURN_STATUS_FAILED
            session = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
            )
            assert session.blocked
            command = await stub.GetCommand(
                sampler_pb2.GetCommandRequest(session_id=request.session_id, command_id="start-ack-lost"),
                metadata=METADATA,
            )
            assert command.outcome == sampler_pb2.COMMAND_OUTCOME_UNKNOWN

    assert proxy.dropped.is_set()
    assert proxy.effect_observed.is_set()
    assert len(proxy.initializations) == 1
    assert (tmp_path / "fault-marker").read_text() == "x"
    starts = [
        call
        for call in proxy.requests
        if call.get("method") == "process/start" and "fault-marker" in json.dumps(call.get("params", {}).get("argv"))
    ]
    assert len(starts) == 1
    process_id = starts[0]["params"]["processId"]
    assert not any(
        call.get("method") == "process/read" and call.get("params", {}).get("processId") == process_id
        for call in proxy.requests
    )
    start_intents = [
        intent
        for intent in await store.intents()
        if intent.request.get("method") == "process/start"
        and "fault-marker" in json.dumps(intent.request.get("params", {}).get("argv"))
    ]
    assert len(start_intents) == 1
    assert start_intents[0].result is None


if __name__ == "__main__":
    pytest_bazel.main()

# Generated service stubs require their direct typing dependencies.
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf
