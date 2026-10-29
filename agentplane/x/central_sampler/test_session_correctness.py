"""Failure and interrupt settlement preserve safe subsequent turns."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.native.codex.testing.responses_sse import CustomToolCall, FunctionCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
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


if __name__ == "__main__":
    pytest_bazel.main()

# Generated service stubs require their direct typing dependencies.
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf
