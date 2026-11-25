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
from itertools import pairwise
from pathlib import Path
from typing import Any, TypeVar, override

import grpc
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from sqlalchemy import text
from tenacity import AsyncRetrying, Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.runtime.execution import executor_pb2, executor_pb2_grpc
from agentplane.grpc.serving import serving
from agentplane.native.codex.testing.responses_sse import CustomToolCall, FunctionCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.proto import event_pb2
from agentplane.sampler.api import sampler_admin_pb2, sampler_pb2
from agentplane.sampler.settings import Settings
from agentplane.sampler.testing import Endpoint, create_request, service, submit_input, turn_completed
from agentplane.sampler.testing_store import IntentSnapshot, Store
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep //agentplane/sampler:testing_fixtures
pytest_plugins = ("agentplane.sampler.testing_fixtures",)

ResponseT = TypeVar("ResponseT")


def _store_retries() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    )


def wait_for_host_pid(directory: Path) -> int:
    pid_path = directory / "code-mode-host.pid"
    for attempt in Retrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert pid_path.exists(), "isolated code-mode host did not become ready"
    return int(pid_path.read_text().strip())


@dataclass
class ExecutorProxy:
    url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    dropped: asyncio.Event = field(default_factory=asyncio.Event)
    effect_observed: asyncio.Event = field(default_factory=asyncio.Event)
    next_read_after_output: asyncio.Event = field(default_factory=asyncio.Event)
    prior_output_seen: bool = False
    dropped_request: dict[str, Any] | None = None
    dropped_reply: dict[str, Any] | None = None
    dropped_count: int = 0


def is_process_start(call: dict[str, Any], marker: str) -> bool:
    return call.get("method") == "StartProcess" and marker in json.dumps(call.get("argv", []))


@asynccontextmanager
async def faulting_executor_proxy(
    execution_target: str, *, drop_method: str, marker: str, drop_count: int = 1, prior_output_marker: str | None = None
) -> AsyncIterator[ExecutorProxy]:
    """Forward typed RPCs to the real adapter and lose one completed gRPC reply."""
    observed = ExecutorProxy(url="")

    def as_dict(value: Any) -> dict[str, Any]:
        return MessageToDict(value, preserving_proto_field_name=True)

    def is_target(request: dict[str, Any], response: Any) -> bool:
        if drop_method == "process/start":
            return is_process_start(request, marker)
        if drop_method == "process/read":
            target_processes = {
                start.get("process_id") for start in observed.requests if is_process_start(start, marker)
            }
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
            if name == "ReadProcess" and observed.prior_output_seen:
                observed.next_read_after_output.set()
            response = await upstream_call(request)
            response_value = {"method": name, **as_dict(response)}
            observed.replies.append(response_value)
            if (
                isinstance(response, executor_pb2.ReadProcessResponse)
                and prior_output_marker
                and any(prior_output_marker.encode() in chunk.data for chunk in response.chunks)
            ):
                observed.prior_output_seen = True
            if is_target(request_value, response) and observed.dropped_count < drop_count:
                observed.dropped_request = request_value
                observed.dropped_reply = response_value
                observed.dropped_count += 1
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


def find_started_process_id(proxy: ExecutorProxy, marker: str) -> str:
    """The process started by the one `StartProcess` request whose argv carries `marker`."""
    starts = [call for call in proxy.requests if is_process_start(call, marker)]
    assert len(starts) == 1
    process_id: str = starts[0]["process_id"]
    return process_id


async def find_start_intent(store: Store, marker: str) -> IntentSnapshot:
    """The one `process/start` intent whose argv carries `marker`."""
    intents = [
        intent
        for intent in await store.intents()
        if intent.request.get("method") == "process/start"
        and marker in json.dumps(intent.request.get("params", {}).get("argv"))
    ]
    assert len(intents) == 1
    return intents[0]


async def assert_process_abandoned(store: Store, process_id: str) -> None:
    process = await store.process(int(process_id.split("-", maxsplit=1)[0]))
    assert process is not None
    assert process.status == "abandoned"


def assert_reply_loss_recovered(proxy: ExecutorProxy) -> None:
    """The proxy lost one completed reply and the sampler resumed the same executor session."""
    assert proxy.dropped.is_set()
    assert proxy.effect_observed.is_set()
    assert len(proxy.sessions) == 2
    executor_session_id = proxy.sessions[0][1]["execution_session_id"]
    assert "resume_session_id" not in proxy.sessions[0][0]
    assert proxy.sessions[1][0]["resume_session_id"] == executor_session_id
    assert proxy.sessions[1][1]["execution_session_id"] == executor_session_id


async def assert_lost_start_ack_settled(proxy: ExecutorProxy, store: Store, marker: str) -> None:
    """The start whose ack was lost ran once, was never read or replayed, and left an abandoned process."""
    assert_reply_loss_recovered(proxy)
    process_id = find_started_process_id(proxy, marker)
    assert not any(
        call.get("method") == "ReadProcess" and call.get("process_id") == process_id for call in proxy.requests
    )
    start_intent = await find_start_intent(store, marker)
    assert start_intent.result is not None
    assert start_intent.result["remote_outcome"] == "unknown"
    assert start_intent.result["automatic_replay"] is False
    await assert_process_abandoned(store, process_id)
    assert all(intent.result is not None for intent in await store.intents())


@pytest.mark.parametrize("failure", ["permanent", "failed", "429", "503", "partial", "invalid_wait"])
async def test_turn_outcomes_preserve_safe_continuation(
    endpoint: Endpoint, config: Settings, store: Store, tmp_path: Path, failure: str
) -> None:
    async with service(config) as stub:
        request = create_request(tmp_path)
        await stub.CreateSession(request)
        await submit_input(stub, session_id=request.session_id, command_id="first", text="Start", timeout_s=None)
        if failure == "invalid_wait":
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream(
                        [
                            FunctionCall("bad-wait", "wait", {"cell_id": "missing"}),
                            CustomToolCall(
                                "sibling", "exec", 'text(await tools.exec_command({cmd:"touch sibling-ran"}));'
                            ),
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [
                    item
                    for item in exchange.request.body["input"]
                    if item["type"] in {"custom_tool_call_output", "function_call_output"}
                ]
                assert {item["call_id"] for item in outputs} == {"bad-wait", "sibling"}
                bad_wait = next(item for item in outputs if item["call_id"] == "bad-wait")
                assert "known live cell_id" in json.dumps(bad_wait["output"])
                assert (tmp_path / "sibling-ran").exists()
                await exchange.send(
                    *response_stream([Message("Continued after tool error")], model="test-model").events
                )
            assert (await turn_completed(stub, request.session_id, "first")).status == event_pb2.TURN_STATUS_COMPLETED
            observed = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
            assert not observed.blocked
            assert not any(intent.result is None for intent in await store.intents())
            return

        attempts = 1 if failure in {"permanent", "failed"} else 3
        frozen_body: dict[str, Any] | None = None
        for _ in range(attempts):
            async with await endpoint.next() as exchange:
                if frozen_body is None:
                    frozen_body = exchange.request.body
                else:
                    assert exchange.request.body == frozen_body, "provider retry changed the frozen request"

                if failure in {"permanent", "429", "503"}:
                    status = 400 if failure == "permanent" else int(failure)
                    await exchange.respond(JsonResponse(b'{"error":{"code":"rate_limit_exceeded"}}', status))
                elif failure == "failed":
                    payload = {
                        "type": "response.failed",
                        "response": {"status": "failed", "error": {"code": "rate_limit_exceeded"}},
                    }
                    await exchange.send(SseEvent("response.failed", ("data: " + json.dumps(payload) + "\n\n").encode()))
                else:
                    events = response_stream(
                        [
                            CustomToolCall(
                                "partial-provider-call",
                                "exec",
                                'text(await tools.exec_command({cmd:"touch partial-provider-call-ran"}));',
                            )
                        ],
                        model="test-model",
                    ).through("response.output_item.done")
                    await exchange.send(*events.events)
                    await exchange.close()

        assert frozen_body is not None
        assert json.dumps(frozen_body["input"]).count('"Start"') == 1
        assert (await turn_completed(stub, request.session_id, "first")).status == event_pb2.TURN_STATUS_FAILED
        observed = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
        assert not observed.blocked
        command = await stub.admin.GetCommand(
            sampler_admin_pb2.GetCommandRequest(session_id=request.session_id, command_id="first")
        )
        assert command.outcome == sampler_pb2.COMMAND_OUTCOME_FAILED
        assert command.failure_reason
        intents = await store.intents()
        async with store.engine.connect() as connection:
            attempt_rows = (
                await connection.execute(
                    text(
                        "SELECT id::text, provider_attempt_no, provider_attempt_budget "
                        "FROM sampler_intent WHERE provider_command_id=:command_id "
                        "AND request->>'kind'='sampling' ORDER BY provider_attempt_no"
                    ),
                    {"command_id": "first"},
                )
            ).all()
        intents_by_id = {intent.id: intent for intent in intents}
        attempts_for_command = [intents_by_id[row.id] for row in attempt_rows]
        assert len(attempts_for_command) == attempts
        assert [row.provider_attempt_no for row in attempt_rows] == list(range(1, attempts + 1))
        assert all(row.provider_attempt_budget == 3 for row in attempt_rows)
        failure_class = "transient" if failure in {"429", "503", "partial"} else "permanent"
        assert all(
            intent.result is not None
            and intent.result.get("status") == "abandoned"
            and intent.result.get("failure_class") == failure_class
            and intent.result.get("failure_reason")
            for intent in attempts_for_command
        )
        if failure == "partial":
            assert not (tmp_path / "partial-provider-call-ran").exists()
        await submit_input(stub, session_id=request.session_id, command_id="second", text="Continue", timeout_s=None)
        async with await endpoint.next() as exchange:
            next_input = json.dumps(exchange.request.body["input"])
            assert next_input.count('"Start"') == 1
            assert next_input.count('"Continue"') == 1
            assert "partial-provider-call" not in next_input
            await exchange.send(*response_stream([Message("Recovered")], model="test-model").events)
        assert (await turn_completed(stub, request.session_id, "second")).status == event_pb2.TURN_STATUS_COMPLETED
        observed = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
        assert not observed.blocked


async def test_lost_process_read_reply_resumes_same_process_and_cursor(
    endpoint: Endpoint, config: Settings, store: Store, execution_target: str, tmp_path: Path
) -> None:
    output = "RECOVERED_PROCESS_OUTPUT"
    async with faulting_executor_proxy(execution_target, drop_method="process/read", marker=output) as proxy:
        config.direct_executor_target = proxy.url
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request)
            await submit_input(
                stub, session_id=request.session_id, command_id="read-recovery", text="Run one command.", timeout_s=None
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
                await turn_completed(stub, request.session_id, "read-recovery")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
            assert not session.blocked

    assert_reply_loss_recovered(proxy)
    assert (tmp_path / "fault-marker").read_text() == "x"
    process_id = find_started_process_id(proxy, "fault-marker")
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
    assert (await find_start_intent(store, "fault-marker")).result is not None
    assert all(intent.result is not None for intent in await store.intents())


async def test_lost_process_read_after_observed_output_is_catchable_without_replay(
    endpoint: Endpoint, config: Settings, store: Store, execution_target: str, tmp_path: Path
) -> None:
    observed_output = "OBSERVED_BEFORE_FAILURE"
    lost_output = "FAULT_PAGE_MARKER"
    command = (
        "printf x >> fault-marker; printf "
        + observed_output
        + "; while [ ! -e release-fault-page ]; do sleep 0.05; done; printf "
        + lost_output
    )
    async with faulting_executor_proxy(
        execution_target,
        drop_method="process/read",
        marker=lost_output,
        drop_count=2,
        prior_output_marker=observed_output,
    ) as proxy:
        config.direct_executor_target = proxy.url
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="observed-read-fault",
                text="Run one command.",
                timeout_s=None,
            )
            async with await endpoint.next() as exchange:
                source = (
                    "try { await tools.exec_command({cmd: " + json.dumps(command) + ", yield_time_ms: 10000}); } "
                    "catch (error) { const failure = JSON.parse(String(error)); "
                    "text(JSON.stringify(failure)); text(failure.observed_output.text); text(failure.message); } "
                )
                await exchange.send(
                    *response_stream([CustomToolCall("observed-read-fault", "exec", source)], model="test-model").events
                )

            # Release the second page only after the sampler has consumed the first one.
            async with asyncio.timeout(15):
                await proxy.next_read_after_output.wait()
            (tmp_path / "release-fault-page").touch()

            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                assert len(outputs) == 1
                failures = [
                    json.loads(part["text"]) for part in outputs[0]["output"] if part["text"].startswith('{"message":')
                ]
                assert len(failures) == 1
                failure = failures[0]
                assert failure["observed_output"]["text"] == observed_output
                assert "remote effect outcome is unknown" in failure["message"]
                assert failure["remote_outcome"] == "unknown"
                assert failure["automatic_replay"] is False
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "follow-up-after-read-fault",
                                "exec",
                                'text(await tools.exec_command({cmd: "printf y >> continued-marker"}));',
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                assert "follow-up-after-read-fault" in {item["call_id"] for item in outputs}
                await exchange.send(
                    *response_stream([Message("continued after observed read fault")], model="test-model").events
                )
            assert (
                await turn_completed(stub, request.session_id, "observed-read-fault")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
            assert not session.blocked

            tool_intents = [
                intent
                for intent in await store.intents()
                if intent.result is not None and intent.result.get("buffered_output_base64")
            ]
            assert len(tool_intents) == 1
            result = tool_intents[0].result
            assert result is not None
            assert base64.b64decode(result["buffered_output_base64"]) == observed_output.encode()
            assert result["remote_outcome"] == "unknown"
            assert result["automatic_replay"] is False
            assert result["after_seq"] > 0
            assert failure["after_seq"] == result["after_seq"]
            assert failure["intent_id"] == result["intent_id"]
            stream = stub.admin.FollowEvents(sampler_admin_pb2.FollowEventsRequest(session_id=request.session_id))
            boundaries = [
                frame.entry.event.boundary_record
                async for frame in stream
                if frame.entry.event.HasField("boundary_record")
                and frame.entry.event.boundary_record.boundary == "tool"
                and frame.entry.event.boundary_record.direction == "result"
            ]
            assert any(
                json.loads(boundary.payload).get("buffered_output_base64") == result["buffered_output_base64"]
                for boundary in boundaries
            )

    assert proxy.dropped_count == 2
    assert (tmp_path / "fault-marker").read_text() == "x"
    assert (tmp_path / "continued-marker").read_text() == "y"
    await assert_process_abandoned(store, find_started_process_id(proxy, lost_output))
    intents = await store.intents()
    assert any(intent.id == failure["intent_id"] and intent.result is not None for intent in intents)
    assert all(intent.result is not None for intent in intents)


async def test_lost_process_start_ack_rejects_tool_and_caught_call_continues(
    endpoint: Endpoint, config: Settings, store: Store, execution_target: str, tmp_path: Path
) -> None:
    output = "START_EFFECT_OBSERVED"
    async with faulting_executor_proxy(execution_target, drop_method="process/start", marker=output) as proxy:
        config.direct_executor_target = proxy.url
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="start-ack-lost",
                text="Run one command.",
                timeout_s=None,
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
                await turn_completed(stub, request.session_id, "start-ack-lost")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
            assert not session.blocked

    assert (tmp_path / "fault-marker").read_text() == "x"
    assert (tmp_path / "continued-marker").read_text() == "y"
    await assert_lost_start_ack_settled(proxy, store, "fault-marker")


async def test_uncaught_lost_apply_patch_ack_reaches_model_and_thread_continues(
    endpoint: Endpoint, config: Settings, store: Store, execution_target: str, tmp_path: Path
) -> None:
    marker = "APPLY_PATCH_EFFECT_OBSERVED"
    patch = "*** Begin Patch\n*** Add File: applied.txt\n+" + marker + "\n*** End Patch"
    async with faulting_executor_proxy(execution_target, drop_method="process/start", marker=marker) as proxy:
        config.direct_executor_target = proxy.url
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="uncaught-patch",
                text="Apply one patch.",
                timeout_s=None,
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
                await turn_completed(stub, request.session_id, "uncaught-patch")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
            assert not session.blocked

    assert (tmp_path / "applied.txt").read_text() == marker + "\n"
    assert (tmp_path / "continued-marker").read_text() == "continued"
    await assert_lost_start_ack_settled(proxy, store, marker)


async def test_host_loss_settles_and_resets_javascript_session(
    endpoint: Endpoint, config: Settings, store: Store, tmp_path: Path
) -> None:
    host_logs = undeclared_outputs_dir() / "recovery-host-loss"
    # Isolate this service's host so the test can deliberately terminate it without
    # affecting another test's shared fixture or host process.
    config.logs = host_logs
    async with service(config) as stub:
        request = create_request(tmp_path)
        await stub.CreateSession(request)
        await submit_input(
            stub, session_id=request.session_id, command_id="recover-start", text="Start the operation.", timeout_s=None
        )
        async with await endpoint.next() as exchange:
            source = 'store("recovery-state", "old-state"); await new Promise(() => {});'
            await exchange.send(
                *response_stream([CustomToolCall("faulted-cell", "exec", source)], model="test-model").events
            )

        host_pid = await asyncio.to_thread(wait_for_host_pid, host_logs)
        async for attempt in _store_retries():
            with attempt:
                assert await store.has_live_cell(request.session_id), "the faulted cell never became live"
        os.killpg(host_pid, signal.SIGKILL)

        assert (await turn_completed(stub, request.session_id, "recover-start")).status == event_pb2.TURN_STATUS_FAILED
        # The restarted host has a new instance, so the work scan selects the blocked Session
        # and its next owner settles the lost cell without an operator.
        async for attempt in _store_retries():
            with attempt:
                settled = await stub.admin.GetSession(
                    sampler_admin_pb2.GetSessionRequest(session_id=request.session_id)
                )
                assert not settled.blocked, f"{settled=}"
        abandoned = [
            intent
            for intent in await store.intents()
            if intent.result is not None and intent.result.get("status") == "abandoned"
        ]
        assert {intent.request.get("kind") for intent in abandoned} == {"cell"}
        assert all(intent.result is not None and intent.result.get("automatic_replay") is False for intent in abandoned)

        # Operator recovery stays authenticated and has nothing left to recover.
        recovery = sampler_admin_pb2.RecoverSessionRequest(
            session_id=request.session_id,
            expected_cursor=settled.attached.last_cursor,
            operator_note="Nothing to review; the loss settled on its own.",
        )
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await stub.admin.RecoverSession(recovery, metadata=(("authorization", "Bearer recreated-account-token"),))
        assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED
        with pytest.raises(grpc.aio.AioRpcError) as nothing_to_recover:
            await stub.admin.RecoverSession(recovery)
        assert nothing_to_recover.value.code() == grpc.StatusCode.FAILED_PRECONDITION

        await submit_input(
            stub,
            session_id=request.session_id,
            command_id="continue-after-recovery",
            text="Continue safely.",
            timeout_s=None,
        )
        async with await endpoint.next() as exchange:
            prompt = json.dumps(exchange.request.body["input"])
            assert "faulted-cell" in prompt
            assert "replayed" in prompt
            assert "store()/load()" in prompt
            assert "live-cell handles are unavailable" in prompt
            await exchange.send(
                *response_stream(
                    [CustomToolCall("check-reset", "exec", 'text(String(load("recovery-state")));')], model="test-model"
                ).events
            )
        async with await endpoint.next() as exchange:
            assert "undefined" in json.dumps(exchange.request.body["input"])
            await exchange.send(*response_stream([Message("continued after recovery")], model="test-model").events)
        assert (
            await turn_completed(stub, request.session_id, "continue-after-recovery")
        ).status == event_pb2.TURN_STATUS_COMPLETED


async def test_stale_json_exec_cell_id_is_a_tool_error_with_live_process(
    endpoint: Endpoint, config: Settings, store: Store, tmp_path: Path
) -> None:
    release = tmp_path / "release-background-process"
    sampled_inputs: list[list[dict[str, Any]]] = []

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

    config = config.model_copy(
        update={"models": {"test-model": config.models["test-model"].model_copy(update={"exec_encoding": "json"})}}
    )
    async with service(config) as stub:
        request = create_request(tmp_path)
        await stub.CreateSession(request)
        await submit_input(
            stub,
            session_id=request.session_id,
            command_id="stale-json-cell",
            text="Start a process and continue.",
            timeout_s=None,
        )

        process_id: int | None = None
        try:
            async with await endpoint.next() as exchange:
                sampled_inputs.append(exchange.request.body["input"])
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
                    *response_stream([FunctionCall("start-process", "exec", {"code": code})], model="test-model").events
                )

            async with await endpoint.next() as exchange:
                sampled_inputs.append(exchange.request.body["input"])
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
                sampled_inputs.append(exchange.request.body["input"])
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
                sampled_inputs.append(exchange.request.body["input"])
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
                await turn_completed(stub, request.session_id, "stale-json-cell")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            observed = await stub.admin.GetSession(sampler_admin_pb2.GetSessionRequest(session_id=request.session_id))
            assert not observed.blocked
            assert not any(intent.result is None for intent in await store.intents())
            process = await store.process(process_id)
            assert process is not None
            assert process.status == "running"

            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="after-stale-json-cell",
                text="Continue once more.",
                timeout_s=None,
            )
            async with await endpoint.next() as exchange:
                sampled_inputs.append(exchange.request.body["input"])
                assert f"process={process_id}" in function_output_text(exchange.request.body, "valid-after-stale")
                await exchange.send(
                    *response_stream([Message("The next input still works.")], model="test-model").events
                )
            assert (
                await turn_completed(stub, request.session_id, "after-stale-json-cell")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            for earlier, later in pairwise(sampled_inputs):
                assert later[: len(earlier)] == earlier
            snapshot_counts = [json.dumps(items).count("Active execution handles: ") for items in sampled_inputs]
            assert snapshot_counts == [0, 0, 0, 0, 0]
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
