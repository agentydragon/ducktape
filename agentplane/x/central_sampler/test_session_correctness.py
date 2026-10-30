"""Failure and interrupt settlement preserve safe subsequent turns."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

import grpc
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict

from agentplane.grpc.serving import serving
from agentplane.native.codex.testing.responses_sse import CustomToolCall, FunctionCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.proto import event_pb2, execution_pb2
from agentplane.runner import runner_exec_pb2_grpc, runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.central_sampler.testing_store import Store
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

    class Proxy(runner_exec_pb2_grpc.RunnerExecServicer):
        @override
        async def OpenSession(
            self, request: execution_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
        ) -> AsyncIterator[execution_pb2.OpenSessionResponse]:
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
            upstream_call: Callable[..., Awaitable[Any]],
        ) -> Any:
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
            self, request: execution_pb2.StartProcessRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.StartProcessResponse:
            return await self.forward("StartProcess", request, context, upstream.StartProcess)

        @override
        async def ReadProcess(
            self, request: execution_pb2.ReadProcessRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.ReadProcessResponse:
            return await self.forward("ReadProcess", request, context, upstream.ReadProcess)

        @override
        async def WriteProcess(
            self, request: execution_pb2.WriteProcessRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.WriteProcessResponse:
            return await self.forward("WriteProcess", request, context, upstream.WriteProcess)

        @override
        async def SignalProcess(
            self, request: execution_pb2.SignalProcessRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.SignalProcessResponse:
            return await self.forward("SignalProcess", request, context, upstream.SignalProcess)

        @override
        async def TerminateProcess(
            self, request: execution_pb2.TerminateProcessRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.TerminateProcessResponse:
            return await self.forward("TerminateProcess", request, context, upstream.TerminateProcess)

        @override
        async def GetFileMetadata(
            self, request: execution_pb2.GetFileMetadataRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.GetFileMetadataResponse:
            return await self.forward("GetFileMetadata", request, context, upstream.GetFileMetadata)

        @override
        async def OpenFile(
            self, request: execution_pb2.OpenFileRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.OpenFileResponse:
            return await self.forward("OpenFile", request, context, upstream.OpenFile)

        @override
        async def ReadFile(
            self, request: execution_pb2.ReadFileRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.ReadFileResponse:
            return await self.forward("ReadFile", request, context, upstream.ReadFile)

        @override
        async def CloseFile(
            self, request: execution_pb2.CloseFileRequest, context: grpc.aio.ServicerContext
        ) -> execution_pb2.CloseFileResponse:
            return await self.forward("CloseFile", request, context, upstream.CloseFile)

    async with grpc.aio.insecure_channel(execution_target) as upstream_channel:
        upstream = runner_exec_pb2_grpc.RunnerExecStub(upstream_channel)
        runner_exec_pb2_grpc.add_RunnerExecServicer_to_server(Proxy(), server)
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


if __name__ == "__main__":
    pytest_bazel.main()

# Generated service stubs require their direct typing dependencies.
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf
