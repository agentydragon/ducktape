"""Failure and interrupt settlement preserve safe subsequent turns."""

import asyncio
import base64
import json
import os
import re
import shlex
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, override

import grpc
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict

from agentplane.executor import executor_pb2, executor_pb2_grpc
from agentplane.grpc.serving import serving
from agentplane.native.codex.testing.responses_sse import CustomToolCall, FunctionCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.central_sampler.testing_store import Store
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir

ResponseT = TypeVar("ResponseT")


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


def wait_for_host_pid(directory: Path) -> int:
    deadline = time.monotonic() + 15
    pid_path = directory / "code-mode-host.pid"
    while time.monotonic() < deadline:
        if pid_path.exists():
            return int(pid_path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError("isolated code-mode host did not become ready")


@dataclass
class ExecutorProxy:
    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    dropped: asyncio.Event = field(default_factory=asyncio.Event)
    effect_observed: asyncio.Event = field(default_factory=asyncio.Event)
    dropped_request: dict[str, Any] | None = None
    dropped_reply: dict[str, Any] | None = None


@asynccontextmanager
async def faulting_executor_proxy(
    execution_target: str, *, drop_method: str, marker: str
) -> AsyncIterator[ExecutorProxy]:
    """Forward typed RPCs to the real adapter and lose one completed gRPC reply."""
    observed = ExecutorProxy(url="")

    def as_dict(value: Any) -> dict[str, Any]:
        return MessageToDict(value, preserving_proto_field_name=True)

    def is_target_start(request: dict[str, Any]) -> bool:
        return request.get("method") == "StartProcess" and marker in json.dumps(request.get("argv", []))

    def is_target(request: dict[str, Any], response: Any) -> bool:
        if drop_method == "process/start":
            return is_target_start(request)
        if drop_method == "process/read":
            target_processes = {start.get("process_id") for start in observed.requests if is_target_start(start)}
            return (
                request.get("method") == "ReadProcess"
                and request.get("process_id") in target_processes
                and any(marker.encode() in chunk.data for chunk in response.chunks)
            )
        raise AssertionError(f"unsupported fault injection method: {drop_method}")

    server = grpc.aio.server()

    class Proxy(executor_pb2_grpc.ExecutorServicer):
        @override
        async def OpenSession(
            self, request: executor_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[executor_pb2.OpenSessionResponse]:
            request_value = as_dict(request)
            async for response in upstream.OpenSession(request):
                response_value = as_dict(response)
                if response.HasField("status"):
                    observed.sessions.append((request_value, response_value))
                yield response

        async def forward(
            self,
            name: str,
            request: Any,
            context: grpc.aio.ServicerContext,
            upstream_call: Callable[..., Awaitable[ResponseT]],
        ) -> ResponseT:
            request_value = {"method": name, **as_dict(request)}
            observed.requests.append(request_value)
            response = await upstream_call(request)
            response_value = {"method": name, **as_dict(response)}
            observed.replies.append(response_value)
            if is_target(request_value, response) and not observed.dropped.is_set():
                observed.dropped_request = request_value
                observed.dropped_reply = response_value
                observed.effect_observed.set()
                observed.dropped.set()
                await context.abort(
                    grpc.StatusCode.UNAVAILABLE, "injected lost typed gRPC reply after adapter completion"
                )
            return response

        @override
        async def StartProcess(
            self, request: executor_pb2.StartProcessRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.StartProcessResponse:
            return await self.forward("StartProcess", request, context, upstream.StartProcess)

        @override
        async def ReadProcess(
            self, request: executor_pb2.ReadProcessRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.ReadProcessResponse:
            return await self.forward("ReadProcess", request, context, upstream.ReadProcess)

        @override
        async def WriteProcess(
            self, request: executor_pb2.WriteProcessRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.WriteProcessResponse:
            return await self.forward("WriteProcess", request, context, upstream.WriteProcess)

        @override
        async def SignalProcess(
            self, request: executor_pb2.SignalProcessRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.SignalProcessResponse:
            return await self.forward("SignalProcess", request, context, upstream.SignalProcess)

        @override
        async def TerminateProcess(
            self, request: executor_pb2.TerminateProcessRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.TerminateProcessResponse:
            return await self.forward("TerminateProcess", request, context, upstream.TerminateProcess)

        @override
        async def GetFileMetadata(
            self, request: executor_pb2.GetFileMetadataRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.GetFileMetadataResponse:
            return await self.forward("GetFileMetadata", request, context, upstream.GetFileMetadata)

        @override
        async def OpenFile(
            self, request: executor_pb2.OpenFileRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.OpenFileResponse:
            return await self.forward("OpenFile", request, context, upstream.OpenFile)

        @override
        async def ReadFile(
            self, request: executor_pb2.ReadFileRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.ReadFileResponse:
            return await self.forward("ReadFile", request, context, upstream.ReadFile)

        @override
        async def CloseFile(
            self, request: executor_pb2.CloseFileRequest, context: grpc.aio.ServicerContext
        ) -> executor_pb2.CloseFileResponse:
            return await self.forward("CloseFile", request, context, upstream.CloseFile)

    async with grpc.aio.insecure_channel(execution_target) as upstream_channel:
        upstream = executor_pb2_grpc.ExecutorStub(upstream_channel)
        executor_pb2_grpc.add_ExecutorServicer_to_server(Proxy(), server)
        async with serving(server, "127.0.0.1:0", grace_s=0) as port:
            observed.url = f"127.0.0.1:{port}"
            yield observed


@pytest.mark.parametrize("failure", ["failed", "429", "503", "unsupported", "invalid_wait", "partial"])
async def test_failed_turn_preserves_safe_continuation(
    store: Store,
    database_url: str,
    execution_target: str,
    codex_binary: str,
    code_mode_host: str,
    tmp_path: Path,
    failure: str,
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
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
    store: Store, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    output = "RECOVERED_PROCESS_OUTPUT"
    async with (
        Endpoint() as endpoint,
        faulting_executor_proxy(execution_target, drop_method="process/read", marker=output) as proxy,
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
        if call.get("method") == "StartProcess" and "fault-marker" in json.dumps(call.get("argv"))
    ]
    assert len(starts) == 1
    process_id = starts[0]["process_id"]
    reads = [
        call for call in proxy.requests if call.get("method") == "ReadProcess" and call.get("process_id") == process_id
    ]
    assert proxy.dropped_request is not None
    first_read = reads.index(proxy.dropped_request)
    assert reads[first_read + 1] == proxy.dropped_request
    assert len([call for call in reads if call["request_id"] == proxy.dropped_request["request_id"]]) == 2
    assert proxy.dropped_reply is not None
    dropped_chunks = proxy.dropped_reply["chunks"]
    assert output.encode() in b"".join(base64.b64decode(chunk["data"]) for chunk in dropped_chunks)
    assert len(proxy.sessions) == 2
    executor_session_id = proxy.sessions[0][1]["execution_session_id"]
    assert "resume_session_id" not in proxy.sessions[0][0]
    assert proxy.sessions[1][0]["resume_session_id"] == executor_session_id
    assert proxy.sessions[1][1]["execution_session_id"] == executor_session_id
    assert all(intent.result is not None for intent in await store.intents())


async def test_lost_process_start_ack_rejects_tool_and_caught_call_continues(
    store: Store, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    output = "START_EFFECT_OBSERVED"
    async with (
        Endpoint() as endpoint,
        faulting_executor_proxy(execution_target, drop_method="process/start", marker=output) as proxy,
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
                source = (
                    'store("sentinel", "before"); '
                    'try { await tools.exec_command({cmd: "printf x >> fault-marker && printf ' + output + '"}); } '
                    'catch (error) { store("lost-start-error", String(error)); } '
                    'await tools.exec_command({cmd: "printf y >> continued-marker"}); '
                    'text(load("sentinel")); text(load("lost-start-error"));'
                )
                await exchange.send(
                    *response_stream([CustomToolCall("start-ack-lost", "exec", source)], model="test-model").events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                assert "before" in json.dumps(outputs)
                assert "remote effect outcome is unknown" in json.dumps(outputs)
                assert "process" in json.dumps(outputs)
                await exchange.send(
                    *response_stream([Message("caught tool error and continued")], model="test-model").events
                )
            assert (
                await wait_turn(stub, request.session_id, "start-ack-lost")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
            )
            assert not session.blocked

    assert proxy.dropped.is_set()
    assert proxy.effect_observed.is_set()
    assert len(proxy.sessions) == 2
    assert (tmp_path / "fault-marker").read_text() == "x"
    starts = [
        call
        for call in proxy.requests
        if call.get("method") == "StartProcess" and "fault-marker" in json.dumps(call.get("argv"))
    ]
    assert len(starts) == 1
    process_id = starts[0]["process_id"]
    assert (tmp_path / "continued-marker").read_text() == "y"
    assert not any(
        call.get("method") == "ReadProcess" and call.get("process_id") == process_id for call in proxy.requests
    )
    start_intents = [
        intent
        for intent in await store.intents()
        if intent.request.get("method") == "process/start"
        and "fault-marker" in json.dumps(intent.request.get("params", {}).get("argv"))
    ]
    assert len(start_intents) == 1
    assert start_intents[0].result is not None
    assert start_intents[0].result["remote_outcome"] == "unknown"
    assert start_intents[0].result["automatic_replay"] is False
    process = await store.process(int(process_id.split("-", maxsplit=1)[0]))
    assert process is not None
    assert process.status == "abandoned"
    assert all(intent.result is not None for intent in await store.intents())
    assert len(proxy.sessions) == 2
    executor_session_id = proxy.sessions[0][1]["execution_session_id"]
    assert proxy.sessions[1][0]["resume_session_id"] == executor_session_id
    assert proxy.sessions[1][1]["execution_session_id"] == executor_session_id


async def test_uncaught_lost_apply_patch_ack_reaches_model_and_thread_continues(
    store: Store, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    marker = "APPLY_PATCH_EFFECT_OBSERVED"
    patch = "*** Begin Patch\n*** Add File: applied.txt\n+" + marker + "\n*** End Patch"
    async with (
        Endpoint() as endpoint,
        faulting_executor_proxy(execution_target, drop_method="process/start", marker=marker) as proxy,
    ):
        config = settings(
            database_url, endpoint.origin, proxy.url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="uncaught-patch", text="Apply one patch."
                ),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                source = 'store("uncaught-sentinel", "persisted"); await tools.apply_patch(' + json.dumps(patch) + ");"
                await exchange.send(
                    *response_stream([CustomToolCall("uncaught-patch", "exec", source)], model="test-model").events
                )
            async with await endpoint.next() as exchange:
                failed = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                assert "Script failed" in json.dumps(failed)
                assert "remote effect outcome is unknown" in json.dumps(failed)
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "after-uncaught",
                                "exec",
                                'text(load("uncaught-sentinel")); await tools.exec_command({cmd: "printf continued >> continued-marker"});',
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                assert "persisted" in json.dumps(outputs)
                await exchange.send(
                    *response_stream([Message("continued after uncaught error")], model="test-model").events
                )
            assert (
                await wait_turn(stub, request.session_id, "uncaught-patch")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
            )
            assert not session.blocked

    assert proxy.dropped.is_set()
    assert proxy.effect_observed.is_set()
    assert (tmp_path / "applied.txt").read_text() == marker + "\n"
    assert (tmp_path / "continued-marker").read_text() == "continued"
    starts = [
        call
        for call in proxy.requests
        if call.get("method") == "StartProcess" and marker in json.dumps(call.get("argv"))
    ]
    assert len(starts) == 1
    process_id = starts[0]["process_id"]
    assert not any(
        call.get("method") == "ReadProcess" and call.get("process_id") == process_id for call in proxy.requests
    )
    patch_intents = [
        intent
        for intent in await store.intents()
        if intent.request.get("method") == "process/start"
        and marker in json.dumps(intent.request.get("params", {}).get("argv"))
    ]
    assert len(patch_intents) == 1
    assert patch_intents[0].result is not None
    assert patch_intents[0].result["remote_outcome"] == "unknown"
    assert patch_intents[0].result["automatic_replay"] is False
    process = await store.process(int(process_id.split("-", maxsplit=1)[0]))
    assert process is not None
    assert process.status == "abandoned"
    assert all(intent.result is not None for intent in await store.intents())
    assert len(proxy.sessions) == 2
    executor_session_id = proxy.sessions[0][1]["execution_session_id"]
    assert proxy.sessions[1][0]["resume_session_id"] == executor_session_id
    assert proxy.sessions[1][1]["execution_session_id"] == executor_session_id


async def test_explicit_recovery_resets_javascript_session_after_host_loss(
    store: Store, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    host_logs = undeclared_outputs_dir() / "recovery-host-loss"
    async with Endpoint() as endpoint:
        # Isolate this service's host so the test can deliberately terminate it without
        # affecting another test's shared fixture or host process.
        config = settings(database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, host_logs)
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="recover-start", text="Start the operation."
                ),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                source = 'store("recovery-state", "old-state"); await new Promise(() => {});'
                await exchange.send(
                    *response_stream([CustomToolCall("faulted-cell", "exec", source)], model="test-model").events
                )

            host_pid = await asyncio.to_thread(wait_for_host_pid, host_logs)
            async with asyncio.timeout(15):
                while not await store.has_live_cell(request.session_id):
                    await asyncio.to_thread(time.sleep, 0.05)
            os.killpg(host_pid, signal.SIGKILL)

            assert (await wait_turn(stub, request.session_id, "recover-start")).status == event_pb2.TURN_STATUS_FAILED
            async with asyncio.timeout(15):
                while True:
                    blocked = await stub.GetSession(
                        sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
                    )
                    if blocked.blocked and await store.idle_generation(request.session_id) is not None:
                        break
                    await asyncio.to_thread(time.sleep, 0.05)
            assert blocked.blocked

            recovery = sampler_pb2.RecoverSessionRequest(
                session_id=request.session_id,
                expected_cursor=blocked.attached.last_cursor,
                operator_note="Inspected the live cell and boundary evidence; recreate in-memory state before continuing.",
                replacement_incarnation_uid="reviewed-replacement-incarnation",
            )
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.RecoverSession(recovery, metadata=(("authorization", "Bearer recreated-account-token"),))
            assert rejected.value.code() == grpc.StatusCode.PERMISSION_DENIED

            recovered = await stub.RecoverSession(recovery, metadata=METADATA)
            assert not recovered.blocked
            assert recovered.attached.last_cursor > blocked.attached.last_cursor
            assert recovered.expected_incarnation_uid == "reviewed-replacement-incarnation"
            assert recovered.executor == blocked.executor
            assert await store.idle_generation(request.session_id) is not None
            abandoned = [
                intent
                for intent in await store.intents()
                if intent.result is not None and intent.result.get("status") == "abandoned"
            ]
            assert {intent.request.get("kind") for intent in abandoned} == {"cell"}
            assert all(
                intent.result is not None and intent.result.get("automatic_replay") is False for intent in abandoned
            )

            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="continue-after-recovery", text="Continue safely."
                ),
                metadata=METADATA,
            )
            async with await endpoint.next() as exchange:
                prompt = json.dumps(exchange.request.body["input"])
                assert "faulted-cell" in prompt
                assert "not replayed" in prompt.lower()
                assert "store()/load()" in prompt
                assert "new JavaScript session" in prompt
                assert "live-cell handles are unavailable" in prompt
                await exchange.send(
                    *response_stream(
                        [CustomToolCall("check-reset", "exec", 'text(String(load("recovery-state")));')],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                assert "undefined" in json.dumps(exchange.request.body["input"])
                await exchange.send(*response_stream([Message("continued after recovery")], model="test-model").events)
            assert (
                await wait_turn(stub, request.session_id, "continue-after-recovery")
            ).status == event_pb2.TURN_STATUS_COMPLETED


async def test_stale_json_exec_cell_id_is_a_tool_error_with_live_process(
    store: Store, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    release = tmp_path / "release-background-process"

    def function_output_text(body: dict[str, Any], call_id: str) -> str:
        matches = [
            item for item in body["input"] if item["type"] == "function_call_output" and item["call_id"] == call_id
        ]
        assert len(matches) == 1
        output = matches[0]["output"]
        if isinstance(output, list):
            return "\n".join(part["text"] for part in output)
        assert isinstance(output, str)
        return output

    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config = config.model_copy(
            update={"models": {"test-model": config.models["test-model"].model_copy(update={"exec_encoding": "json"})}}
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="stale-json-cell", text="Start a process and continue."
                ),
                metadata=METADATA,
            )

            process_id: int | None = None
            try:
                async with await endpoint.next() as exchange:
                    process_arguments = {
                        "cmd": f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.01; done; printf retained-output",
                        "yield_time_ms": 1,
                    }
                    code = (
                        'store("validation-state", "preserved"); '
                        f"const process = await tools.exec_command({json.dumps(process_arguments)}); "
                        'store("validation-process", process.session_id); '
                        'text("process=" + process.session_id);'
                    )
                    await exchange.send(
                        *response_stream(
                            [FunctionCall("start-process", "exec", {"code": code})], model="test-model"
                        ).events
                    )

                async with await endpoint.next() as exchange:
                    start_output = function_output_text(exchange.request.body, "start-process")
                    match = re.search(r"process=(\d+)", start_output)
                    assert match is not None, start_output
                    process_id = int(match[1])
                    process = await store.process(process_id)
                    assert process is not None
                    assert process.status == "running"
                    before_invalid = await store.get_session(request.session_id)
                    await exchange.send(
                        *response_stream(
                            [
                                FunctionCall(
                                    "stale-cell-id",
                                    "exec",
                                    {"cell_id": "00000000-0000-4000-8000-000000000001", "terminate": True},
                                )
                            ],
                            model="test-model",
                        ).events
                    )

                async with await endpoint.next() as exchange:
                    invalid_output = function_output_text(exchange.request.body, "stale-cell-id")
                    assert "known live cell_id" in invalid_output
                    assert "process session_id is not accepted" in invalid_output
                    recent_events = await store.events(request.session_id, before_invalid.cursor)
                    assert not any(
                        entry.event.HasField("boundary_record")
                        and entry.event.boundary_record.boundary == "javascript"
                        and entry.event.boundary_record.direction == "send"
                        and b"session/terminate" in entry.event.boundary_record.payload
                        for entry in recent_events
                    )
                    assert not any(
                        entry.event.HasField("boundary_record")
                        and entry.event.boundary_record.boundary == "executor"
                        and entry.event.boundary_record.direction == "dispatch"
                        for entry in recent_events
                    )
                    process = await store.process(process_id)
                    assert process is not None
                    assert process.status == "running"
                    correct_code = (
                        'const saved = load("validation-state"); '
                        'const processId = load("validation-process"); '
                        "const current = await tools.write_stdin({session_id: processId, yield_time_ms: 0}); "
                        'if (saved !== "preserved" || current.session_id !== processId) '
                        'throw new Error("live JavaScript or process handle was lost"); '
                        'text("state=" + saved + " process=" + current.session_id);'
                    )
                    await exchange.send(
                        *response_stream(
                            [FunctionCall("valid-after-stale", "exec", {"code": correct_code})], model="test-model"
                        ).events
                    )

                async with await endpoint.next() as exchange:
                    valid_output = function_output_text(exchange.request.body, "valid-after-stale")
                    assert "Script completed" in valid_output
                    assert "state=preserved" in valid_output
                    assert f"process={process_id}" in valid_output
                    process = await store.process(process_id)
                    assert process is not None
                    assert process.status == "running"
                    await exchange.send(
                        *response_stream([Message("Continued after the invalid cell ID.")], model="test-model").events
                    )

                assert (
                    await wait_turn(stub, request.session_id, "stale-json-cell")
                ).status == event_pb2.TURN_STATUS_COMPLETED
                observed = await stub.GetSession(
                    sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA
                )
                assert not observed.blocked
                assert not any(intent.result is None for intent in await store.intents())
                process = await store.process(process_id)
                assert process is not None
                assert process.status == "running"

                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=request.session_id, command_id="after-stale-json-cell", text="Continue once more."
                    ),
                    metadata=METADATA,
                )
                async with await endpoint.next() as exchange:
                    active_handles = json.dumps(exchange.request.body["input"])
                    assert "Active execution handles" in active_handles
                    assert str(process_id) in active_handles
                    await exchange.send(
                        *response_stream([Message("The next input still works.")], model="test-model").events
                    )
                assert (
                    await wait_turn(stub, request.session_id, "after-stale-json-cell")
                ).status == event_pb2.TURN_STATUS_COMPLETED
            finally:
                release.write_text("")
                if process_id is not None:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        process = await store.process(process_id)
                        if process is not None and process.status != "running":
                            break
                        await asyncio.sleep(0.05)


if __name__ == "__main__":
    pytest_bazel.main()

# Generated service stubs require their direct typing dependencies.
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf
