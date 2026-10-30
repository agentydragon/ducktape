"""Standalone API with real Postgres, V8 host and executor; no Runner or History."""

import asyncio
import base64
import io
import json
import re
import shlex
import textwrap
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

import grpc
import pytest
import pytest_bazel
from PIL import Image
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.native.codex.testing.responses_sse import (
    CustomToolCall,
    FunctionCall,
    Message,
    Reasoning,
    response_stream,
)
from agentplane.native.testing.model_endpoint import SseEvent
from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.testing.codex_fixtures import ExecutorServer
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.settings import ModelConfig, ResponsesConfig
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.central_sampler.testing_store import Store
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def completed(
    stub: sampler_pb2_grpc.SamplerAsyncStub, turn_id: str, session_id: str = "079bd706-77b3-4b71-8c7e-605a7e19324c"
) -> event_pb2.TurnCompleted:
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(15):
            async for frame in stream:
                if frame.entry.event.HasField("turn_completed") and frame.entry.event.turn_completed.turn_id == turn_id:
                    return frame.entry.event.turn_completed
    finally:
        stream.cancel()
    raise AssertionError("event stream ended")


async def test_flat_model_routes_enforce_effort_and_session_contract(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.models["restricted-model"] = config.models["test-model"].model_copy(
            update={"reasoning_efforts": {"high"}}
        )
        config.models["compatible-model"] = config.models["test-model"].model_copy(update={"reasoning_efforts": {""}})
        config.models["standard-model"] = ModelConfig(
            reasoning_efforts={"", "high"},
            api="responses",
            responses=ResponsesConfig(dialect="standard"),
            exec_encoding="json",
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            request.spec.model = "unknown-model"
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            request.spec.model = "restricted-model"
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            request.spec.model = "test-model"
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="hold-turn", text="Wait for the response."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                for model, code in (
                    ("unknown-model", grpc.StatusCode.INVALID_ARGUMENT),
                    ("restricted-model", grpc.StatusCode.INVALID_ARGUMENT),
                    ("standard-model", grpc.StatusCode.FAILED_PRECONDITION),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as rejected:
                        await stub.ChangeModel(
                            runner_service_pb2.ChangeModelRequest(
                                session_id=request.session_id, command_id=model, model=model
                            ),
                            metadata=METADATA,
                            timeout=10,
                        )
                    assert rejected.value.code() == code
                await stub.ChangeModel(
                    runner_service_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="compatible", model="compatible-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                with pytest.raises(grpc.aio.AioRpcError) as rejected:
                    await stub.ChangeReasoningEffort(
                        runner_service_pb2.ChangeReasoningEffortRequest(
                            session_id=request.session_id, command_id="unsupported-effort", effort="high"
                        ),
                        metadata=METADATA,
                        timeout=10,
                    )
                assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
                await stub.ChangeModel(
                    runner_service_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="restore-model", model="test-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                await stub.ChangeReasoningEffort(
                    runner_service_pb2.ChangeReasoningEffortRequest(
                        session_id=request.session_id, command_id="high-effort", effort="high"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                await stub.ChangeModel(
                    runner_service_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="restricted", model="restricted-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                with pytest.raises(grpc.aio.AioRpcError) as rejected:
                    await stub.ChangeModel(
                        runner_service_pb2.ChangeModelRequest(
                            session_id=request.session_id, command_id="incompatible-effort", model="compatible-model"
                        ),
                        metadata=METADATA,
                        timeout=10,
                    )
                assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
                await exchange.send(*response_stream([Message("done")], model="test-model").events)
            assert (await completed(stub, "hold-turn")).status == event_pb2.TURN_STATUS_COMPLETED


async def test_session_store_survives_idle_leases_and_is_separate_between_sessions(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            first = open_request(tmp_path)
            second = open_request(tmp_path)
            second.session_id = "ad901ef2-5cf6-443a-b456-f15fa74c9b07"
            for request in (first, second):
                await stub.OpenSession(request, metadata=METADATA, timeout=10)

            async def run_cell(session_id: str, command_id: str, source: str, expected: str) -> int:
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=session_id, command_id=command_id, text="Run the next cell."
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    await exchange.send(
                        *response_stream([CustomToolCall(command_id, "exec", source)], model="test-model").events
                    )
                async with await endpoint.next() as exchange:
                    outputs = [
                        item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"
                    ]
                    assert expected in json.dumps(outputs[-1])
                    await exchange.send(*response_stream([Message("cell complete")], model="test-model").events)
                assert (await completed(stub, command_id, session_id)).status == event_pb2.TURN_STATUS_COMPLETED
                # Require the driver to release its lease so the next turn binds a new Owner.
                async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
                    with attempt:
                        generation = await store.idle_generation(session_id)
                        assert generation is not None
                        return generation
                raise AssertionError("lease release retry loop ended")

            generation = await run_cell(
                first.session_id, "store-first", 'store("key", "first-value"); text("saved");', "saved"
            )
            await run_cell(second.session_id, "store-second", 'store("key", "second-value"); text("saved");', "saved")
            successor = await run_cell(first.session_id, "load-first", 'text(load("key"));', "first-value")
            assert successor > generation
            await run_cell(second.session_id, "load-second", 'text(load("key"));', "second-value")


async def test_api_wait_stdin_queue_compaction_and_restart(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            opened = await stub.OpenSession(open_request(tmp_path), metadata=METADATA, timeout=10)
            assert opened.attached.session_id == "079bd706-77b3-4b71-8c7e-605a7e19324c"
            request = runner_service_pb2.SubmitInputRequest(
                session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="first", text="Exercise execution. " * 100
            )
            first = await stub.SubmitInput(request, metadata=METADATA, timeout=10)
            assert await stub.SubmitInput(request, metadata=METADATA, timeout=10) == first
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "start",
                                "exec",
                                # Only a TTY process keeps stdin open.
                                'text(await tools.exec_command({cmd: "read line; printf received:%s \\"$line\\"", '
                                "tty: true, yield_time_ms: 1}));",
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                process = json.loads(outputs[-1]["output"][1]["text"])["session_id"]
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "stdin",
                                "exec",
                                '// @exec: {"yield_time_ms": 1}\ntext(await tools.write_stdin('
                                + json.dumps({"session_id": process, "chars": "hello\n", "yield_time_ms": 1000})
                                + "));",
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                match = re.search(r"cell ID ([A-Za-z0-9_:-]+)", str(outputs[-1]))
                if match is not None:
                    await exchange.send(
                        *response_stream(
                            [FunctionCall("wait", "wait", {"cell_id": match[1], "yield_time_ms": 1000})],
                            model="test-model",
                        ).events
                    )
                else:
                    pytest.fail("script did not yield a JS cell")
            async with await endpoint.next() as exchange:
                assert "received:hello" in json.dumps(exchange.request.body)
                # Input admitted while the provider is held steers the running turn.
                queued = await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id="079bd706-77b3-4b71-8c7e-605a7e19324c",
                        command_id="queued",
                        text="Also mention the steering input.",
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                assert queued.admission.cursor > first.admission.cursor
                await exchange.send(*response_stream([Message("first answer")], model="test-model").events)
            async with await endpoint.next() as exchange:
                # Pending input keeps a turn whose model already answered; it follows that answer once.
                body_input = exchange.request.body["input"]
                steering = [
                    index
                    for index, item in enumerate(body_input)
                    if item.get("content") == "Also mention the steering input."
                ]
                assert len(steering) == 1
                answer = next(
                    index
                    for index, item in enumerate(body_input)
                    if item.get("role") == "assistant" and "first answer" in json.dumps(item.get("content"))
                )
                assert answer < steering[0]
                await exchange.send(*response_stream([Message("first complete")], model="test-model").events)
            assert (await completed(stub, "first")).status == event_pb2.TURN_STATUS_COMPLETED
            steered = await stub.GetCommand(
                sampler_pb2.GetCommandRequest(session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="queued"),
                metadata=METADATA,
                timeout=10,
            )
            assert steered.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED
            await stub.Compact(
                sampler_pb2.CompactRequest(session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="compact"),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                assert exchange.request.body["input"][-1] == {"type": "compaction_trigger"}
                payloads: tuple[dict[str, Any], ...] = (
                    {
                        "type": "response.output_item.done",
                        "item": {"type": "compaction", "id": "compact-id", "encrypted_content": "test-opaque"},
                    },
                    {"type": "response.completed", "response": {"id": "compacted"}},
                )
                for payload in payloads:
                    await exchange.send(SseEvent(payload["type"], ("data: " + json.dumps(payload) + "\n\n").encode()))
            stream = stub.FollowEvents(
                sampler_pb2.FollowEventsRequest(session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", follow=True),
                metadata=METADATA,
            )
            try:
                async with asyncio.timeout(10):
                    async for frame in stream:
                        if (
                            frame.entry.event.HasField("execution_progress")
                            and frame.entry.event.execution_progress.operation_id == "compact"
                            and frame.entry.event.execution_progress.phase == "completed"
                        ):
                            break
            finally:
                stream.cancel()
        # Independent runtime instance resumes canonical context and idempotent receipts.
        async with service(config) as stub:
            assert await stub.SubmitInput(request, metadata=METADATA, timeout=10) == first
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="after", text="After compaction"
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                assert any(item.get("encrypted_content") == "test-opaque" for item in exchange.request.body["input"])
                await exchange.send(*response_stream([Message("resumed")], model="test-model").events)
            await completed(stub, "after")
        events = await store.events("079bd706-77b3-4b71-8c7e-605a7e19324c", 0, limit=10000)
        assert [entry.cursor for entry in events] == list(range(1, len(events) + 1))
        assert len({entry.origin.source_id for entry in events}) == 1
        assert any(
            entry.event.HasField("boundary_record") and entry.event.boundary_record.boundary == "provider"
            for entry in events
        )
        assert all(intent.result is not None for intent in await store.intents())


async def test_interrupt_streaming_evidence_and_uid_authorization(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            await stub.OpenSession(open_request(tmp_path), metadata=METADATA, timeout=10)
            for metadata, expected in (
                ((), grpc.StatusCode.UNAUTHENTICATED),
                ((("authorization", "Bearer recreated-account-token"),), grpc.StatusCode.PERMISSION_DENIED),
            ):
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    await stub.GetSession(
                        sampler_pb2.GetSessionRequest(session_id="079bd706-77b3-4b71-8c7e-605a7e19324c"),
                        metadata=metadata,
                        timeout=10,
                    )
                assert error.value.code() == expected
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="active", text="Wait for interrupt"
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                payload = {"type": "response.output_text.delta", "item_id": "partial", "delta": "partial output"}
                await exchange.send(SseEvent(payload["type"], ("data: " + json.dumps(payload) + "\n\n").encode()))
                stream = stub.FollowEvents(
                    sampler_pb2.FollowEventsRequest(session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", follow=True),
                    metadata=METADATA,
                )
                try:
                    async with asyncio.timeout(10):
                        async for frame in stream:
                            if frame.entry.event.HasField("text_delta"):
                                assert frame.entry.event.source_sequences
                                break
                finally:
                    stream.cancel()
                interrupt = runner_service_pb2.InterruptTurnRequest(
                    session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="interrupt", turn_id="active"
                )
                receipt = await stub.InterruptTurn(interrupt, metadata=METADATA, timeout=10)
                assert (await completed(stub, "active")).status == event_pb2.TURN_STATUS_INTERRUPTED
            assert await stub.InterruptTurn(interrupt, metadata=METADATA, timeout=10) == receipt
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id="079bd706-77b3-4b71-8c7e-605a7e19324c",
                        command_id="interrupt",
                        text="different payload",
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
            assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="continued", text="Continue"
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(*response_stream([Message("continued")], model="test-model").events)
            await completed(stub, "continued")


async def test_nested_command_inherits_executor_environment(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="env", text="Show the environment."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream(
                        [CustomToolCall("env", "exec", 'text((await tools.exec_command({cmd: "env"})).output);')],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                text = "\n".join(part["text"] for part in outputs[-1]["output"] if part["type"] == "input_text")
                variables = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
                await exchange.send(*response_stream([Message("done")], model="test-model").events)
            assert (await completed(stub, "env", request.session_id)).status == event_pb2.TURN_STATUS_COMPLETED
    # Inherited from the exec-server's own environment; the Session's variables are set over it.
    assert variables.get("PATH")
    assert variables.get("HOME")
    assert variables.get("AGENTPLANE_DESTINATION")


async def test_reasoning_summaries_are_requested_and_reach_history(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="think", text="Think, then answer."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                reasoning = exchange.request.body["reasoning"]
                await exchange.send(
                    *response_stream(
                        [Reasoning("Weighing the answer.", encrypted_content="test-opaque"), Message("answered")],
                        model="test-model",
                    ).events
                )
            assert (await completed(stub, "think", request.session_id)).status == event_pb2.TURN_STATUS_COMPLETED
        events = [entry.event for entry in await store.events(request.session_id, 0, limit=10000)]
    assert reasoning["summary"] == "auto"
    assert reasoning["context"] == "all_turns"
    reasoning_items = {
        event.item_started.item_id
        for event in events
        if event.HasField("item_started") and event.item_started.kind == event_pb2.ITEM_KIND_REASONING
    }
    summaries = [
        event.item_completed.text
        for event in events
        if event.HasField("item_completed") and event.item_completed.item_id in reasoning_items
    ]
    assert summaries == ["Weighing the answer."]


async def test_restart_never_replays_unresolved_provider_intent(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with AsyncExitStack() as active:
            stub = await active.enter_async_context(service(config))
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            command = runner_service_pb2.SubmitInputRequest(
                session_id=request.session_id, command_id="lost", text="Provider outcome will be lost"
            )
            receipt = await stub.SubmitInput(command, metadata=METADATA, timeout=10)
            async with await endpoint.next():
                intents = await store.intents()
                assert len(intents) == 1
                assert intents[0].result is None
                intent_id = intents[0].id
                await active.aclose()
        async with service(config) as stub:
            terminal = await completed(stub, "lost")
            assert terminal.status == event_pb2.TURN_STATUS_FAILED
            snapshot = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert snapshot.blocked
            assert await stub.SubmitInput(command, metadata=METADATA, timeout=10) == receipt
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=request.session_id, command_id="new", text="Must not replay"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
            assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        intents = await store.intents()
        assert len(intents) == 1
        assert intents[0].id == intent_id
        assert intents[0].result is None


async def test_interrupt_admission_survives_unavailable_executor(
    database_url: str, executor_server: ExecutorServer, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_server.url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="remote", text="Start a supervised process"
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "remote-process",
                                "exec",
                                'text(await tools.exec_command({cmd: "read line", tty: true, yield_time_ms: 1}));',
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next():
                executor_server.process.kill()
                await executor_server.process.wait()
                interrupt = runner_service_pb2.InterruptTurnRequest(
                    session_id=request.session_id, command_id="remote-interrupt", turn_id="remote"
                )
                receipt = await stub.InterruptTurn(interrupt, metadata=METADATA, timeout=10)
                assert receipt.admission.event.command_admitted.command.interrupt_turn.turn_id == "remote"
                assert (await completed(stub, "remote")).status == event_pb2.TURN_STATUS_INTERRUPTED
            session = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert session.blocked
            outcome = await stub.GetCommand(
                sampler_pb2.GetCommandRequest(session_id=request.session_id, command_id="remote-interrupt"),
                metadata=METADATA,
                timeout=10,
            )
            assert outcome.outcome == sampler_pb2.COMMAND_OUTCOME_UNKNOWN


async def test_restart_cannot_reconstruct_live_javascript(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="yielded", text="Retain a live JS cell"
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "live-cell", "exec", '// @exec: {"yield_time_ms": 1}\nawait new Promise(() => {});'
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                assert "Script running with cell ID" in json.dumps(exchange.request.body)
                await exchange.send(*response_stream([Message("cell remains live")], model="test-model").events)
            await completed(stub, "yielded")
        async with service(config) as stub:
            stream = stub.FollowEvents(
                sampler_pb2.FollowEventsRequest(session_id=request.session_id, follow=True), metadata=METADATA
            )
            try:
                async with asyncio.timeout(10):
                    async for frame in stream:
                        if frame.entry.event.execution_progress.phase == "blocked":
                            break
            finally:
                stream.cancel()
            session = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert session.blocked
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=request.session_id, command_id="lost-cell", text="Continue after host loss"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
            assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            intents = [intent for intent in await store.intents() if intent.request.get("kind") == "cell"]
            assert len(intents) == 1


async def test_unpolled_process_exit_releases_capacity_and_retains_output(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.max_live_sessions = 1
        release = tmp_path / "release-process"
        request = open_request(tmp_path)
        second = open_request(tmp_path)
        second.session_id = "11111111-1111-4111-8111-111111111111"
        async with service(config) as stub:
            for session in (request, second):
                await stub.OpenSession(session, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id,
                    command_id="background",
                    text="Start a background process and answer.",
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                arguments = {
                    "cmd": f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.01; done; printf retained-output",
                    "yield_time_ms": 1,
                }
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "background", "exec", f"text(await tools.exec_command({json.dumps(arguments)}));"
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                process_id = json.loads(outputs[-1]["output"][1]["text"])["session_id"]
                await exchange.send(*response_stream([Message("Process started.")], model="test-model").events)
            assert (await completed(stub, "background")).status == event_pb2.TURN_STATUS_COMPLETED
            process = await store.process(process_id)
            assert process is not None
            assert process.status == "running"
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=second.session_id, command_id="queued", text="Run once capacity is released."
                ),
                metadata=METADATA,
                timeout=10,
            )
            release.write_text("")
            async with await endpoint.next() as exchange:
                assert any(
                    item.get("content") == "Run once capacity is released." for item in exchange.request.body["input"]
                )
                await exchange.send(*response_stream([Message("Queued session ran.")], model="test-model").events)
            assert (await completed(stub, "queued", second.session_id)).status == event_pb2.TURN_STATUS_COMPLETED
            process = await store.process(process_id)
            assert process is not None
            assert process.status == "exited_unread"
            assert process.buffered_output == b"retained-output"
            assert process.conversation_owner is None
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="read-retained", text="Read the retained output."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                assert str(process_id) in json.dumps(exchange.request.body)
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "read-retained",
                                "exec",
                                f"text(await tools.write_stdin({json.dumps({'session_id': process_id})}));",
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                result = json.loads(outputs[-1]["output"][1]["text"])
                assert (result["output"], result["exit_code"]) == ("retained-output", 0)
                assert "session_id" not in result
                await exchange.send(*response_stream([Message("Retained output read.")], model="test-model").events)
            assert (await completed(stub, "read-retained")).status == event_pb2.TURN_STATUS_COMPLETED
            process = await store.process(process_id)
            assert process is not None
            assert process.status == "exited"
            assert process.buffered_output == b""


def tool_outputs(body: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """(call_id, text parts) per tool output; cell results lead with Codex's status header part."""
    return [
        (item["call_id"], [part["text"] for part in item["output"]])
        for item in body["input"]
        if item["type"] in {"custom_tool_call_output", "function_call_output"}
    ]


@pytest.mark.parametrize("dialect", ["lite", "standard"])
async def test_json_exec_runs_shell_resumes_and_recovers(
    database_url: str,
    executor_url: str,
    codex_binary: str,
    code_mode_host: str,
    tmp_path: Path,
    dialect: Literal["lite", "standard"],
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config = config.model_copy(
            update={
                "models": {
                    "test-model": ModelConfig(
                        reasoning_efforts={"", "high"},
                        api="responses",
                        responses=ResponsesConfig(dialect=dialect),
                        exec_encoding="json",
                    )
                }
            }
        )
        Image.new("RGB", (1, 1), (255, 0, 0)).save(tmp_path / "small.png")
        request = open_request(tmp_path)
        request.spec.reasoning_effort = ""
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="standard-json", text="Run a shell command."
                ),
                metadata=METADATA,
                timeout=10,
            )

            async with await endpoint.next() as exchange:
                body = exchange.request.body
                tools = body["input"][0]["tools"] if dialect == "lite" else body["tools"]
                assert len(tools) == 1
                assert tools[0]["type"] == "function"
                assert tools[0]["name"] == "exec"
                assert tools[0]["strict"] is False
                parameters = tools[0]["parameters"]
                assert parameters["type"] == "object"
                assert parameters["additionalProperties"] is False
                assert not parameters.get("required")
                assert set(parameters["properties"]) == {
                    "code",
                    "cell_id",
                    "yield_time_ms",
                    "max_output_tokens",
                    "terminate",
                }
                properties = parameters["properties"]
                for name in ("code", "cell_id"):
                    assert properties[name]["type"] == "string"
                for name in ("yield_time_ms", "max_output_tokens"):
                    assert properties[name]["type"] == "integer"
                    assert properties[name]["minimum"] == 0
                assert properties["terminate"]["type"] == "boolean"
                headers = {name.lower(): value for name, value in exchange.headers.items()}
                if dialect == "lite":
                    assert body["reasoning"]["context"] == "all_turns"
                    assert headers["x-openai-internal-codex-responses-lite"] == "true"
                else:
                    assert "reasoning" not in body
                    assert "include" not in body
                    assert "client_metadata" not in body
                    assert not any(item["type"] == "additional_tools" for item in body["input"])
                    assert "x-openai-internal-codex-responses-lite" not in headers

                code = textwrap.dedent(
                    """\
                    text("before shell");
                    notify("shell notification");
                    const result = await tools.exec_command({
                      cmd: "sleep 0.4; printf json-shell-output",
                      yield_time_ms: 1000
                    });
                    text("quoted argument: \\"value\\"");
                    text(result.output);
                    """
                )
                await exchange.send(
                    *response_stream(
                        [
                            FunctionCall(
                                "json-exec", "exec", {"code": code, "yield_time_ms": 100, "max_output_tokens": 1000}
                            )
                        ],
                        model="test-model",
                    ).events
                )

            async with await endpoint.next() as exchange:
                body = exchange.request.body
                call_outputs = [
                    item
                    for item in body["input"]
                    if item["type"] == "function_call_output" and item["call_id"] == "json-exec"
                ]
                assert len(call_outputs) == 1
                if dialect == "lite":
                    assert isinstance(call_outputs[0]["output"], list)
                    call_output_text = "\n".join(part["text"] for part in call_outputs[0]["output"])
                else:
                    assert isinstance(call_outputs[0]["output"], str)
                    call_output_text = call_outputs[0]["output"]
                assert "Script running with cell ID" in call_output_text
                cell_id_match = re.search(r"cell ID ([A-Za-z0-9_:-]+)", call_output_text)
                assert cell_id_match is not None

                # notify() is delivered once as an asynchronous developer message with JSON exec.
                assert not any(
                    item["type"] == "custom_tool_call_output" and item.get("call_id") == "json-exec"
                    for item in body["input"]
                )
                notification_messages = [
                    item
                    for item in body["input"]
                    if item.get("type") == "message"
                    and item.get("role") == "developer"
                    and "shell notification" in str(item.get("content"))
                ]
                assert len(notification_messages) == 1
                await exchange.send(
                    *response_stream(
                        [
                            FunctionCall(
                                "json-resume",
                                "exec",
                                {"cell_id": cell_id_match[1], "yield_time_ms": 1000, "max_output_tokens": 1000},
                            )
                        ],
                        model="test-model",
                    ).events
                )

            async with await endpoint.next() as exchange:
                resumed = [
                    item
                    for item in exchange.request.body["input"]
                    if item["type"] == "function_call_output" and item["call_id"] == "json-resume"
                ]
                assert len(resumed) == 1
                if dialect == "lite":
                    assert isinstance(resumed[0]["output"], list)
                    resumed_text = "\n".join(part["text"] for part in resumed[0]["output"])
                else:
                    assert isinstance(resumed[0]["output"], str)
                    resumed_text = resumed[0]["output"]
                assert "Script completed" in resumed_text
                assert 'quoted argument: "value"' in resumed_text
                assert "json-shell-output" in resumed_text
                await exchange.send(
                    *response_stream(
                        [FunctionCall("json-error", "exec", {"code": 'throw new Error("recoverable script error");'})],
                        model="test-model",
                    ).events
                )

            async with await endpoint.next() as exchange:
                failed = [
                    item
                    for item in exchange.request.body["input"]
                    if item["type"] == "function_call_output" and item["call_id"] == "json-error"
                ]
                assert len(failed) == 1
                if dialect == "lite":
                    assert isinstance(failed[0]["output"], list)
                    failed_text = "\n".join(part["text"] for part in failed[0]["output"])
                else:
                    assert isinstance(failed[0]["output"], str)
                    failed_text = failed[0]["output"]
                assert "Script failed" in failed_text
                assert "recoverable script error" in failed_text
                await exchange.send(
                    *response_stream(
                        [
                            FunctionCall(
                                "json-image",
                                "exec",
                                {"code": 'const viewed = await tools.view_image({path: "small.png"}); image(viewed);'},
                            )
                        ],
                        model="test-model",
                    ).events
                )

            async with await endpoint.next() as exchange:
                image_output = [
                    item
                    for item in exchange.request.body["input"]
                    if item["type"] == "function_call_output" and item["call_id"] == "json-image"
                ]
                assert len(image_output) == 1
                assert isinstance(image_output[0]["output"], list)
                assert any(part["type"] == "input_image" for part in image_output[0]["output"])
                await exchange.send(
                    *response_stream(
                        [Message("Recovered after the script error and saw the image.")], model="test-model"
                    ).events
                )

            assert (
                await completed(stub, "standard-json", request.session_id)
            ).status == event_pb2.TURN_STATUS_COMPLETED


async def test_notify_outputs_follow_the_step_that_emitted_them(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="notify", text="Notify from cells."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream(
                        [CustomToolCall("completed", "exec", 'notify("progress"); text("final");')], model="test-model"
                    ).events
                )
            async with await endpoint.next() as exchange:
                (exec_call, exec_parts), notification = tool_outputs(exchange.request.body)
                assert exec_call == "completed"
                assert exec_parts[0].startswith("Script completed")
                assert exec_parts[1:] == ["final"]
                assert notification == ("completed", ["progress"])
                source = (
                    '// @exec: {"yield_time_ms": 10}\n'
                    "await new Promise((resolve) => setTimeout(resolve, 1500));\n"
                    'notify("late");\n'
                    'text("done");'
                )
                await exchange.send(
                    *response_stream([CustomToolCall("yielded", "exec", source)], model="test-model").events
                )
            async with await endpoint.next() as exchange:
                outputs = tool_outputs(exchange.request.body)
                assert "late" not in json.dumps(outputs)
                match = re.search(r"cell ID ([A-Za-z0-9_:-]+)", outputs[-1][1][0])
                assert match is not None
                await exchange.send(
                    *response_stream(
                        [FunctionCall("wait", "wait", {"cell_id": match[1], "yield_time_ms": 10000})],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                # The yielded cell's notification belongs to its exec call but follows the wait output.
                (wait_call, wait_parts), notification = tool_outputs(exchange.request.body)[-2:]
                assert wait_call == "wait"
                assert wait_parts[0].startswith("Script completed")
                assert wait_parts[1:] == ["done"]
                assert notification == ("yielded", ["late"])
                await exchange.send(*response_stream([Message("notified")], model="test-model").events)
            assert (await completed(stub, "notify", request.session_id)).status == event_pb2.TURN_STATUS_COMPLETED


async def test_interrupt_discards_queued_notify_output(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="interrupted", text="Notify, then get interrupted."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                # The cell notifies only once the test releases it, after the next step is held.
                source = (
                    '// @exec: {"yield_time_ms": 10}\n'
                    'await tools.exec_command({cmd: "while [ ! -e release ]; do sleep 0.05; done", yield_time_ms: 30000});\n'
                    'notify("discarded");\n'
                    "await new Promise(() => {});"
                )
                await exchange.send(
                    *response_stream([CustomToolCall("pending", "exec", source)], model="test-model").events
                )
            async with await endpoint.next() as exchange:
                # Holding this sampling step keeps the notification queued until the interrupt.
                (tmp_path / "release").touch()
                async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
                    with attempt:
                        assert await store.queued_notifications(request.session_id) == ["discarded"]
                await stub.InterruptTurn(
                    runner_service_pb2.InterruptTurnRequest(
                        session_id=request.session_id, command_id="interrupt", turn_id="interrupted"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                assert (
                    await completed(stub, "interrupted", request.session_id)
                ).status == event_pb2.TURN_STATUS_INTERRUPTED
            assert await store.queued_notifications(request.session_id) == []
        settled = [
            entry.event.boundary_record.direction
            for entry in await store.events(request.session_id, 0, limit=10000)
            if entry.event.boundary_record.boundary == "notification"
        ]
        assert settled == ["discarded"]


async def test_view_image_reads_executor_file_and_prepares_cell_image(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    oversized = io.BytesIO()
    Image.new("RGB", (3000, 1000), (255, 0, 0)).save(oversized, format="PNG")
    (tmp_path / "oversized.png").write_bytes(oversized.getvalue())
    (tmp_path / "notes.txt").write_text("not an image")
    source = textwrap.dedent(
        """\
        const viewed = await tools.view_image({path: "oversized.png"});
        text(viewed.detail);
        image(viewed);
        for (const path of ["missing.png", "notes.txt"]) {
          try {
            await tools.view_image({path});
          } catch {
            text(`rejected ${path}`);
          }
        }
        """
    )
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="view-image", text="Look at the image."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream([CustomToolCall("view-image", "exec", source)], model="test-model").events
                )
            async with await endpoint.next() as exchange:
                (output,) = [
                    item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"
                ]
                # The first part is the cell's status header.
                texts = [part["text"] for part in output["output"][1:] if part["type"] == "input_text"]
                assert texts == ["high", "rejected missing.png", "rejected notes.txt"]
                (shown,) = [part for part in output["output"] if part["type"] == "input_image"]
                header, encoded = shown["image_url"].split(",", 1)
                assert header == "data:image/png;base64"
                # 3000x1000 exceeds high detail's 2048-pixel bound; the aspect ratio is kept.
                assert Image.open(io.BytesIO(base64.b64decode(encoded))).size == (2048, 683)
                await exchange.send(*response_stream([Message("Seen.")], model="test-model").events)
            assert (await completed(stub, "view-image")).status == event_pb2.TURN_STATUS_COMPLETED
            # Nested read evidence keeps digests, not the file's bytes.
            file_base64 = base64.b64encode(oversized.getvalue()).decode()
            assert not [
                intent for intent in await Store(engine).intents() if file_base64 in f"{intent.request}{intent.result}"
            ]


async def test_input_yields_a_running_cell_and_joins_the_turn(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="steered", text="Run a long cell."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                # The cell never settles and would hold its observation for its whole yield time.
                source = '// @exec: {"yield_time_ms": 300000}\nawait new Promise(() => {});'
                await exchange.send(
                    *response_stream([CustomToolCall("long", "exec", source)], model="test-model").events
                )
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="steer", text="Read this instead."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                body_input = exchange.request.body["input"]
                (call_id, parts) = tool_outputs(exchange.request.body)[-1]
                assert call_id == "long"
                match = re.search(r"cell ID ([A-Za-z0-9_:-]+)", parts[0])
                assert match is not None
                steering = [
                    index for index, item in enumerate(body_input) if item.get("content") == "Read this instead."
                ]
                assert len(steering) == 1
                exec_output = next(
                    index for index, item in enumerate(body_input) if item.get("call_id") == "long" and "output" in item
                )
                assert exec_output < steering[0]
                await exchange.send(
                    *response_stream(
                        [FunctionCall("stop", "wait", {"cell_id": match[1], "terminate": True})], model="test-model"
                    ).events
                )
            async with await endpoint.next() as exchange:
                await exchange.send(*response_stream([Message("steered")], model="test-model").events)
            assert (await completed(stub, "steered", request.session_id)).status == event_pb2.TURN_STATUS_COMPLETED
            steer = await stub.GetCommand(
                sampler_pb2.GetCommandRequest(session_id=request.session_id, command_id="steer"),
                metadata=METADATA,
                timeout=10,
            )
            assert steer.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED


if __name__ == "__main__":
    pytest_bazel.main()

# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
