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

from agentplane.harness_host import harness_host_pb2
from agentplane.native.codex.testing.responses_sse import (
    CustomToolCall,
    FunctionCall,
    Message,
    Reasoning,
    response_stream,
)
from agentplane.native.testing.model_endpoint import SseEvent
from agentplane.proto import event_pb2
from agentplane.sampler.api import sampler_admin_pb2, sampler_pb2
from agentplane.sampler.settings import ChatCompactionConfig, ChatCompactionReplacement, ModelConfig, ResponsesConfig
from agentplane.sampler.testing import (
    METADATA,
    SESSION_ID,
    Endpoint,
    create_request,
    service,
    settings,
    submit_input,
    turn_completed,
)
from agentplane.sampler.testing_store import Store
from agentplane.testing.codex_fixtures import ExecutorServer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.sqlalchemy_engines import postgres_async_engine


async def test_flat_model_routes_enforce_effort_and_session_contract(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.models["restricted-model"] = config.models["test-model"].model_copy(
            update={"reasoning_efforts": {"high"}}
        )
        config.models["compatible-model"] = config.models["test-model"].model_copy(update={"reasoning_efforts": {""}})
        config.models["other-provider-model"] = config.models["test-model"].model_copy(
            update={"provider_model": "other-test-model"}
        )
        config.models["standard-model"] = ModelConfig(
            connection="provider",
            provider_model="standard-provider-model",
            reasoning_efforts={"", "high"},
            api="responses",
            responses=ResponsesConfig(dialect="standard"),
            exec_encoding="json",
        )
        async with service(config) as stub:
            request = create_request(tmp_path)
            request.spec.model = "unknown-model"
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.CreateSession(request, metadata=METADATA, timeout=10)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            request.spec.model = "restricted-model"
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.CreateSession(request, metadata=METADATA, timeout=10)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            request.spec.model = "test-model"
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="hold-turn", text="Wait for the response.", timeout_s=10
            )
            async with await endpoint.next() as exchange:
                for model, code in (
                    ("unknown-model", grpc.StatusCode.INVALID_ARGUMENT),
                    ("restricted-model", grpc.StatusCode.INVALID_ARGUMENT),
                    ("standard-model", grpc.StatusCode.FAILED_PRECONDITION),
                    ("other-provider-model", grpc.StatusCode.FAILED_PRECONDITION),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as rejected:
                        await stub.ChangeModel(
                            harness_host_pb2.ChangeModelRequest(
                                session_id=request.session_id, command_id=model, model=model
                            ),
                            metadata=METADATA,
                            timeout=10,
                        )
                    assert rejected.value.code() == code
                await stub.ChangeModel(
                    harness_host_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="compatible", model="compatible-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                with pytest.raises(grpc.aio.AioRpcError) as rejected:
                    await stub.ChangeReasoningEffort(
                        harness_host_pb2.ChangeReasoningEffortRequest(
                            session_id=request.session_id, command_id="unsupported-effort", effort="high"
                        ),
                        metadata=METADATA,
                        timeout=10,
                    )
                assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
                await stub.ChangeModel(
                    harness_host_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="restore-model", model="test-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                await stub.ChangeReasoningEffort(
                    harness_host_pb2.ChangeReasoningEffortRequest(
                        session_id=request.session_id, command_id="high-effort", effort="high"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                await stub.ChangeModel(
                    harness_host_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="restricted", model="restricted-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                with pytest.raises(grpc.aio.AioRpcError) as rejected:
                    await stub.ChangeModel(
                        harness_host_pb2.ChangeModelRequest(
                            session_id=request.session_id, command_id="incompatible-effort", model="compatible-model"
                        ),
                        metadata=METADATA,
                        timeout=10,
                    )
                assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
                await exchange.send(
                    *response_stream(
                        [
                            Reasoning("A retained summary.", encrypted_content="fixture-model-bound-reasoning"),
                            Message("done"),
                        ],
                        model="test-model",
                    ).events
                )
            assert (await turn_completed(stub, SESSION_ID, "hold-turn")).status == event_pb2.TURN_STATUS_COMPLETED
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.ChangeModel(
                    harness_host_pb2.ChangeModelRequest(
                        session_id=request.session_id, command_id="after-reasoning", model="other-provider-model"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
            assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            # A route alias for the same provider model still accepts retained continuation.
            await stub.ChangeModel(
                harness_host_pb2.ChangeModelRequest(
                    session_id=request.session_id, command_id="same-provider-model", model="test-model"
                ),
                metadata=METADATA,
                timeout=10,
            )
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="continue",
                text="Continue with the same model.",
                timeout_s=10,
            )
            async with await endpoint.next() as exchange:
                assert exchange.request.body["model"] == "test-model"
                assert [
                    item["encrypted_content"] for item in exchange.request.body["input"] if item["type"] == "reasoning"
                ] == ["fixture-model-bound-reasoning"]
                await exchange.send(*response_stream([Message("continued")], model="test-model").events)
            assert (await turn_completed(stub, SESSION_ID, "continue")).status == event_pb2.TURN_STATUS_COMPLETED


async def test_session_store_survives_idle_turns_and_is_separate_between_sessions(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            first = create_request(tmp_path)
            second = create_request(tmp_path)
            second.session_id = "ad901ef2-5cf6-443a-b456-f15fa74c9b07"
            for request in (first, second):
                await stub.CreateSession(request, metadata=METADATA, timeout=10)

            async def run_cell(session_id: str, command_id: str, source: str, expected: str) -> int:
                await submit_input(
                    stub, session_id=session_id, command_id=command_id, text="Run the next cell.", timeout_s=10
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
                assert (await turn_completed(stub, session_id, command_id)).status == event_pb2.TURN_STATUS_COMPLETED
                async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.01), reraise=True):
                    with attempt:
                        generation = await store.resident_generation(session_id)
                        assert generation is not None
                        return generation
                raise AssertionError("resident lease retry loop ended")

            generation = await run_cell(
                first.session_id, "store-first", 'store("key", "first-value"); text("saved");', "saved"
            )
            await run_cell(second.session_id, "store-second", 'store("key", "second-value"); text("saved");', "saved")
            successor = await run_cell(first.session_id, "load-first", 'text(load("key"));', "first-value")
            assert successor == generation
            await run_cell(second.session_id, "load-second", 'text(load("key"));', "second-value")


async def test_api_wait_stdin_queue_compaction_and_restart(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            opened = await stub.CreateSession(create_request(tmp_path), metadata=METADATA, timeout=10)
            assert opened.attached.session_id == SESSION_ID
            request = harness_host_pb2.SubmitInputRequest(
                session_id=SESSION_ID, command_id="first", text="Exercise execution. " * 100
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
                queued = await submit_input(
                    stub,
                    session_id=SESSION_ID,
                    command_id="queued",
                    text="Also mention the steering input.",
                    timeout_s=10,
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
            assert (await turn_completed(stub, SESSION_ID, "first")).status == event_pb2.TURN_STATUS_COMPLETED
            steered = await stub.admin.GetCommand(
                sampler_admin_pb2.GetCommandRequest(session_id=SESSION_ID, command_id="queued"),
                metadata=METADATA,
                timeout=10,
            )
            assert steered.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED
            await stub.admin.Compact(
                sampler_admin_pb2.CompactRequest(session_id=SESSION_ID, command_id="compact"),
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
            stream = stub.admin.FollowEvents(
                sampler_admin_pb2.FollowEventsRequest(session_id=SESSION_ID, follow=True), metadata=METADATA
            )
            try:
                async with asyncio.timeout(10):
                    async for frame in stream:
                        if (
                            frame.entry.event.HasField("execution_progress")
                            and frame.entry.event.execution_progress.operation_id == "compact"
                            and frame.entry.event.execution_progress.phase
                            == event_pb2.ExecutionProgress.PHASE_COMPLETED
                        ):
                            break
            finally:
                stream.cancel()
        # Independent runtime instance resumes canonical context and idempotent receipts.
        async with service(config) as stub:
            assert await stub.SubmitInput(request, metadata=METADATA, timeout=10) == first
            await submit_input(stub, session_id=SESSION_ID, command_id="after", text="After compaction", timeout_s=10)
            async with await endpoint.next() as exchange:
                assert any(item.get("encrypted_content") == "test-opaque" for item in exchange.request.body["input"])
                await exchange.send(*response_stream([Message("resumed")], model="test-model").events)
            await turn_completed(stub, SESSION_ID, "after")
        events = await store.events(SESSION_ID, 0, limit=10000)
        assert [entry.cursor for entry in events] == list(range(1, len(events) + 1))
        assert len({entry.origin.source_id for entry in events}) == 1
        assert any(
            entry.event.HasField("boundary_record") and entry.event.boundary_record.boundary == "provider"
            for entry in events
        )
        intents = await store.intents()
        assert any(intent.request.get("kind") == "sampling" for intent in intents)
        assert sum(intent.request.get("kind") == "compaction" for intent in intents) == 1
        assert all(intent.result is not None for intent in intents)


async def test_interrupt_streaming_evidence_and_service_svid_admission(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            await stub.CreateSession(create_request(tmp_path), metadata=METADATA, timeout=10)
            for metadata, expected in (
                ((("authorization", "Bearer recreated-account-token"),), grpc.StatusCode.UNAUTHENTICATED),
            ):
                with pytest.raises(grpc.aio.AioRpcError) as error:
                    await stub.admin.GetSession(
                        sampler_admin_pb2.GetSessionRequest(session_id=SESSION_ID), metadata=metadata, timeout=10
                    )
                assert error.value.code() == expected
            await submit_input(
                stub, session_id=SESSION_ID, command_id="active", text="Wait for interrupt", timeout_s=10
            )
            async with await endpoint.next() as exchange:
                payload = {"type": "response.output_text.delta", "item_id": "partial", "delta": "partial output"}
                await exchange.send(SseEvent(payload["type"], ("data: " + json.dumps(payload) + "\n\n").encode()))
                stream = stub.admin.FollowEvents(
                    sampler_admin_pb2.FollowEventsRequest(session_id=SESSION_ID, follow=True), metadata=METADATA
                )
                try:
                    async with asyncio.timeout(10):
                        async for frame in stream:
                            if frame.entry.event.HasField("text_delta"):
                                assert frame.entry.event.source_sequences
                                break
                finally:
                    stream.cancel()
                interrupt = harness_host_pb2.InterruptTurnRequest(
                    session_id=SESSION_ID, command_id="interrupt", turn_id="active"
                )
                receipt = await stub.InterruptTurn(interrupt, metadata=METADATA, timeout=10)
                assert (await turn_completed(stub, SESSION_ID, "active")).status == event_pb2.TURN_STATUS_INTERRUPTED
            assert await stub.InterruptTurn(interrupt, metadata=METADATA, timeout=10) == receipt
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await submit_input(
                    stub, session_id=SESSION_ID, command_id="interrupt", text="different payload", timeout_s=10
                )
            assert error.value.code() == grpc.StatusCode.ALREADY_EXISTS
            await submit_input(stub, session_id=SESSION_ID, command_id="continued", text="Continue", timeout_s=10)
            async with await endpoint.next() as exchange:
                await exchange.send(*response_stream([Message("continued")], model="test-model").events)
            await turn_completed(stub, SESSION_ID, "continued")


async def test_nested_command_inherits_executor_environment(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="env", text="Show the environment.", timeout_s=10
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
            assert (await turn_completed(stub, request.session_id, "env")).status == event_pb2.TURN_STATUS_COMPLETED
    # Inherited from the exec-server's own environment; the Session's variables are set over it.
    assert variables.get("PATH")
    assert variables.get("HOME")
    assert variables.get("AGENTPLANE_SESSION_ID") == request.session_id


async def test_reasoning_summaries_are_requested_and_reach_history(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = create_request(tmp_path)
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="think", text="Think, then answer.", timeout_s=10
            )
            async with await endpoint.next() as exchange:
                reasoning = exchange.request.body["reasoning"]
                await exchange.send(
                    *response_stream(
                        [Reasoning("Weighing the answer.", encrypted_content="test-opaque"), Message("answered")],
                        model="test-model",
                    ).events
                )
            assert (await turn_completed(stub, request.session_id, "think")).status == event_pb2.TURN_STATUS_COMPLETED
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


async def test_restart_abandons_cut_provider_request_and_resamples(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        # Exercise the forced drain deadline within this fixture's 10-second process grace.
        config.shutdown_drain_seconds = 1
        config.shutdown_budget_seconds = 16
        async with AsyncExitStack() as active:
            stub = await active.enter_async_context(service(config))
            request = create_request(tmp_path)
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            command = harness_host_pb2.SubmitInputRequest(
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
            # The successor abandons the cut request without committing its stream and samples
            # again; the abandoned request itself is never re-sent.
            async with await endpoint.next() as exchange:
                await exchange.send(*response_stream([Message("resampled")], model="test-model").events)
            terminal = await turn_completed(stub, SESSION_ID, "lost")
            assert terminal.status == event_pb2.TURN_STATUS_COMPLETED
            snapshot = await stub.admin.GetSession(
                sampler_admin_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert not snapshot.blocked
            assert await stub.SubmitInput(command, metadata=METADATA, timeout=10) == receipt
        snapshots = {intent.id: intent for intent in await store.intents()}
        assert len(snapshots) == 2
        abandoned = snapshots[intent_id].result
        assert abandoned is not None
        assert abandoned["status"] == "abandoned"
        assert abandoned["output_committed"] is False
        assert abandoned["automatic_replay"] is False


async def test_interrupt_admission_survives_unavailable_executor(
    database_url: str,
    execution_target: str,
    executor_server: ExecutorServer,
    codex_binary: str,
    code_mode_host: str,
    tmp_path: Path,
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="remote",
                text="Start a supervised process",
                timeout_s=10,
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
                interrupt = harness_host_pb2.InterruptTurnRequest(
                    session_id=request.session_id, command_id="remote-interrupt", turn_id="remote"
                )
                receipt = await stub.InterruptTurn(interrupt, metadata=METADATA, timeout=10)
                assert receipt.admission.event.command_admitted.command.interrupt_turn.turn_id == "remote"
                assert (await turn_completed(stub, SESSION_ID, "remote")).status == event_pb2.TURN_STATUS_INTERRUPTED
            session = await stub.admin.GetSession(
                sampler_admin_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert session.blocked
            outcome = await stub.admin.GetCommand(
                sampler_admin_pb2.GetCommandRequest(session_id=request.session_id, command_id="remote-interrupt"),
                metadata=METADATA,
                timeout=10,
            )
            assert outcome.outcome == sampler_pb2.COMMAND_OUTCOME_UNKNOWN


async def test_restart_settles_live_javascript_and_continues(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = create_request(tmp_path)
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="yielded", text="Retain a live JS cell", timeout_s=10
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
            await turn_completed(stub, SESSION_ID, "yielded")
        async with service(config) as stub:
            # The new host cannot reconstruct the cell; the resident driver that picks the Session
            # up abandons it with a recorded loss and the Session stays usable.
            stream = stub.admin.FollowEvents(
                sampler_admin_pb2.FollowEventsRequest(session_id=request.session_id, follow=True), metadata=METADATA
            )
            try:
                async with asyncio.timeout(10):
                    async for frame in stream:
                        progress = frame.entry.event.execution_progress
                        if (
                            progress.kind == event_pb2.ExecutionProgress.KIND_CODE_HOST_LOSS
                            and progress.phase == event_pb2.ExecutionProgress.PHASE_COMMITTED
                        ):
                            break
            finally:
                stream.cancel()
            session = await stub.admin.GetSession(
                sampler_admin_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert not session.blocked
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="lost-cell",
                text="Continue after host loss",
                timeout_s=10,
            )
            async with await endpoint.next() as exchange:
                assert "store()/load() values has been lost" in json.dumps(exchange.request.body)
                await exchange.send(
                    *response_stream([Message("continued without the cell")], model="test-model").events
                )
            assert (await turn_completed(stub, SESSION_ID, "lost-cell")).status == event_pb2.TURN_STATUS_COMPLETED
            intents = [intent for intent in await store.intents() if intent.request.get("kind") == "cell"]
            assert len(intents) == 1


async def test_exited_process_with_open_output_yields_and_preserves_late_output(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        release = tmp_path / "release-late-output"
        request = create_request(tmp_path)
        async with service(config) as stub:
            try:
                await stub.CreateSession(request, metadata=METADATA, timeout=10)
                await submit_input(
                    stub,
                    session_id=request.session_id,
                    command_id="start-open-output",
                    text="Run the command.",
                    timeout_s=10,
                )
                async with await endpoint.next() as exchange:
                    command = f"(while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.01; done; printf late-output) & printf early-output"
                    arguments = {"cmd": command, "yield_time_ms": 1000}
                    await exchange.send(
                        *response_stream(
                            [
                                CustomToolCall(
                                    "start", "exec", f"text(await tools.exec_command({json.dumps(arguments)}));"
                                )
                            ],
                            model="test-model",
                        ).events
                    )
                async with await endpoint.next() as exchange:
                    outputs = [
                        item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"
                    ]
                    result = json.loads(outputs[-1]["output"][1]["text"])
                    process_id = result["session_id"]
                    assert result["exit_code"] == 0
                    assert result["output"] == "early-output"
                    await exchange.send(*response_stream([Message("The command exited.")], model="test-model").events)
                assert (
                    await turn_completed(stub, request.session_id, "start-open-output")
                ).status == event_pb2.TURN_STATUS_COMPLETED

                # The periodic Session work scan must not become process/read polling.
                await asyncio.sleep(0.3)
                before = sum(intent.request.get("method") == "process/read" for intent in await store.intents())
                await asyncio.sleep(1.3)
                after = sum(intent.request.get("method") == "process/read" for intent in await store.intents())
                assert after == before

                release.write_text("")
                async for attempt in AsyncRetrying(stop=stop_after_delay(5), wait=wait_fixed(0.05), reraise=True):
                    with attempt:
                        process = await store.process(process_id)
                        assert process is not None
                        assert process.status == "exited_unread"
                        assert process.buffered_output == b"late-output"

                await submit_input(
                    stub,
                    session_id=request.session_id,
                    command_id="read-late-output",
                    text="Read the late output.",
                    timeout_s=10,
                )
                async with await endpoint.next() as exchange:
                    arguments = {"session_id": process_id, "yield_time_ms": 1}
                    await exchange.send(
                        *response_stream(
                            [
                                CustomToolCall(
                                    "read", "exec", f"text(await tools.write_stdin({json.dumps(arguments)}));"
                                )
                            ],
                            model="test-model",
                        ).events
                    )
                async with await endpoint.next() as exchange:
                    outputs = [
                        item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"
                    ]
                    result = json.loads(outputs[-1]["output"][1]["text"])
                    assert result["output"] == "late-output"
                    assert result["exit_code"] == 0
                    assert "session_id" not in result
                    await exchange.send(*response_stream([Message("Late output read.")], model="test-model").events)
                assert (
                    await turn_completed(stub, request.session_id, "read-late-output")
                ).status == event_pb2.TURN_STATUS_COMPLETED
            finally:
                release.touch()


async def test_unpolled_process_exit_releases_capacity_and_retains_output(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.max_live_sessions = 1
        release = tmp_path / "release-process"
        request = create_request(tmp_path)
        second = create_request(tmp_path)
        second.session_id = "11111111-1111-4111-8111-111111111111"
        async with service(config) as stub:
            for session in (request, second):
                await stub.CreateSession(session, metadata=METADATA, timeout=10)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="background",
                text="Start a background process and answer.",
                timeout_s=10,
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
            assert (await turn_completed(stub, SESSION_ID, "background")).status == event_pb2.TURN_STATUS_COMPLETED
            process = await store.process(process_id)
            assert process is not None
            assert process.status == "running"
            await submit_input(
                stub,
                session_id=second.session_id,
                command_id="queued",
                text="Run once capacity is released.",
                timeout_s=10,
            )
            release.write_text("")
            async with await endpoint.next() as exchange:
                assert any(
                    item.get("content") == "Run once capacity is released." for item in exchange.request.body["input"]
                )
                await exchange.send(*response_stream([Message("Queued session ran.")], model="test-model").events)
            assert (await turn_completed(stub, second.session_id, "queued")).status == event_pb2.TURN_STATUS_COMPLETED
            process = await store.process(process_id)
            assert process is not None
            assert process.status == "exited_unread"
            assert process.buffered_output == b"retained-output"
            assert process.conversation_owner is not None
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="read-retained",
                text="Read the retained output.",
                timeout_s=10,
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
            assert (await turn_completed(stub, SESSION_ID, "read-retained")).status == event_pb2.TURN_STATUS_COMPLETED
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


async def test_unknown_custom_call_returns_failure_and_preserves_live_state(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = create_request(tmp_path)
        release = tmp_path / "release-unknown-custom-process"
        process_id: int | None = None
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="unknown-custom",
                text="Continue after an unknown tool.",
                timeout_s=10,
            )
            try:
                async with await endpoint.next() as exchange:
                    command = {
                        "cmd": f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.01; done; printf retained-output",
                        "yield_time_ms": 1,
                    }
                    source = (
                        'store("unknown-tool-state", "kept"); '
                        f"const started = await tools.exec_command({json.dumps(command)}); "
                        'store("unknown-tool-process", String(started.session_id)); '
                        "text(JSON.stringify(started));"
                    )
                    await exchange.send(
                        *response_stream([CustomToolCall("start-process", "exec", source)], model="test-model").events
                    )

                async with await endpoint.next() as exchange:
                    started = dict(tool_outputs(exchange.request.body))["start-process"]
                    process_id = json.loads(started[-1])["session_id"]
                    process = await store.process(process_id)
                    assert process is not None
                    assert process.status == "running"
                    source = (
                        'if (load("unknown-tool-state") !== "kept") throw new Error("JavaScript state was lost"); '
                        'const process = await tools.write_stdin({session_id: Number(load("unknown-tool-process")), yield_time_ms: 1}); '
                        'text(JSON.stringify({state: load("unknown-tool-state"), process}));'
                    )
                    await exchange.send(
                        *response_stream(
                            [
                                CustomToolCall("missing-custom", "missing_custom_tool", "unused input"),
                                CustomToolCall("known-sibling", "exec", source),
                            ],
                            model="test-model",
                        ).events
                    )

                async with await endpoint.next() as exchange:
                    items = exchange.request.body["input"]
                    unknown_output = next(
                        item
                        for item in items
                        if item.get("call_id") == "missing-custom" and item["type"].endswith("_output")
                    )
                    assert unknown_output["type"] == "custom_tool_call_output"
                    unknown_text = "\n".join(part["text"] for part in unknown_output["output"])
                    assert unknown_text == "unsupported custom tool call: missing_custom_tool"
                    sibling = dict(tool_outputs(exchange.request.body))["known-sibling"]
                    sibling_result = json.loads(sibling[-1])
                    assert sibling_result["state"] == "kept"
                    process = await store.process(process_id)
                    assert process is not None
                    assert process.status == "running"
                    session = await stub.admin.GetSession(
                        sampler_admin_pb2.GetSessionRequest(session_id=request.session_id),
                        metadata=METADATA,
                        timeout=10,
                    )
                    assert not session.blocked
                    unknown_intents = [
                        intent
                        for intent in await store.intents()
                        if intent.request.get("call", {}).get("call_id") == "missing-custom"
                    ]
                    assert len(unknown_intents) == 1
                    assert unknown_intents[0].result is not None
                    assert unknown_intents[0].result["success"] is False
                    assert unknown_intents[0].result["dispatched"] is False
                    await exchange.send(*response_stream([Message("continued")], model="test-model").events)

                assert (
                    await turn_completed(stub, request.session_id, "unknown-custom")
                ).status == event_pb2.TURN_STATUS_COMPLETED
            finally:
                release.write_text("")

        events = await store.events(request.session_id, 0, limit=10000)
        failed_results = [
            entry.event.item_completed.tool
            for entry in events
            if entry.event.HasField("item_completed") and entry.event.item_completed.item_id == "missing-custom"
        ]
        assert len(failed_results) == 1
        assert not failed_results[0].succeeded
        assert "unsupported custom tool call: missing_custom_tool" in failed_results[0].output


@pytest.mark.parametrize("dialect", ["lite", "standard"])
@pytest.mark.parametrize("function_name", ["missing_function", "wait"])
async def test_unknown_json_function_call_returns_function_output(
    database_url: str,
    execution_target: str,
    codex_binary: str,
    code_mode_host: str,
    tmp_path: Path,
    dialect: Literal["lite", "standard"],
    function_name: str,
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config = config.model_copy(
            update={
                "models": {
                    "test-model": ModelConfig(
                        connection="provider",
                        provider_model="test-model",
                        reasoning_efforts={"", "high"},
                        api="responses",
                        responses=ResponsesConfig(dialect=dialect),
                        exec_encoding="json",
                    )
                }
            }
        )
        request = create_request(tmp_path)
        request.spec.reasoning_effort = ""
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="unknown-function",
                text="Continue after an unknown function.",
                timeout_s=10,
            )
            async with await endpoint.next() as exchange:
                await exchange.send(
                    *response_stream([FunctionCall("missing-function", function_name, {})], model="test-model").events
                )

            async with await endpoint.next() as exchange:
                output = next(
                    item
                    for item in exchange.request.body["input"]
                    if item.get("call_id") == "missing-function" and item["type"].endswith("_output")
                )
                assert output["type"] == "function_call_output"
                if isinstance(output["output"], str):
                    output_text = output["output"]
                else:
                    output_text = "\n".join(part["text"] for part in output["output"])
                assert output_text == f"unsupported call: {function_name}"
                await exchange.send(*response_stream([Message("continued")], model="test-model").events)

            assert (
                await turn_completed(stub, request.session_id, "unknown-function")
            ).status == event_pb2.TURN_STATUS_COMPLETED
            session = await stub.admin.GetSession(
                sampler_admin_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert not session.blocked

        failed_results = [
            entry.event.item_completed.tool
            for entry in await store.events(request.session_id, 0, limit=10000)
            if entry.event.HasField("item_completed") and entry.event.item_completed.item_id == "missing-function"
        ]
        assert len(failed_results) == 1
        assert not failed_results[0].succeeded


async def test_chat_completions_tool_round_trip(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.models["test-model"] = ModelConfig(
            connection="provider",
            provider_model="chat-test-model",
            reasoning_efforts={""},
            api="chat_completions",
            exec_encoding="json",
        )
        request = create_request(tmp_path)
        request.spec.reasoning_effort = ""
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="chat", text="Use the tool.", timeout_s=10
            )
            call_chunk = {
                "id": "chat-tool",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": "Use the tool.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "chat-call",
                                    "type": "function",
                                    "function": {"name": "exec", "arguments": '{"code":"text(42)"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }

            async with await endpoint.next() as exchange:
                initial = exchange.request.body
                assert initial["model"] == "chat-test-model"
                assert {"role": "user", "content": "Use the tool."} in initial["messages"]
                assert initial["tools"][0]["function"]["name"] == "exec"
                # The first stream contains a complete-looking call, then disconnects without [DONE].
                call_event = SseEvent("chunk", b"data: " + json.dumps(call_chunk).encode() + b"\n\n")
                await exchange.send(call_event)

            async with await endpoint.next() as exchange:
                assert exchange.request.body == initial
                await exchange.send(call_event, SseEvent("done", b"data: [DONE]\n\n"))

            async with await endpoint.next() as exchange:
                continued = exchange.request.body
                assistant = next(m for m in continued["messages"] if m["role"] == "assistant")
                tool = next(m for m in continued["messages"] if m["role"] == "tool")
                assert assistant["reasoning_content"] == "Use the tool."
                assert assistant["tool_calls"][0]["id"] == "chat-call"
                assert tool["role"] == "tool"
                assert tool["tool_call_id"] == "chat-call"
                assert "42" in tool["content"]
                await exchange.send(
                    SseEvent(
                        "chunk",
                        b'data: {"id":"chat-done","choices":[{"index":0,"delta":{"content":"Done."},"finish_reason":"stop"}]}\n\n',
                    ),
                    SseEvent("done", b"data: [DONE]\n\n"),
                )
            assert (await turn_completed(stub, request.session_id, "chat")).status == event_pb2.TURN_STATUS_COMPLETED

            async with postgres_async_engine(database_url) as engine:
                entries = await Store(engine).events(request.session_id, 0, limit=10000)
            dispatches = [
                entry.event.execution_progress.operation_id
                for entry in entries
                if entry.event.HasField("execution_progress")
                and entry.event.execution_progress.kind == event_pb2.ExecutionProgress.KIND_SAMPLING
                and entry.event.execution_progress.phase == event_pb2.ExecutionProgress.PHASE_DISPATCH
            ]
            assert len(dispatches) == 3
            completed_tools = [
                entry.event.item_completed
                for entry in entries
                if entry.event.HasField("item_completed") and entry.event.item_completed.HasField("tool")
            ]
            assert [tool.item_id for tool in completed_tools] == ["chat-call"]
            assert completed_tools[0].tool.succeeded
            metadata = None
            body = bytearray()
            async for part in stub.admin.ReadProviderRequest(
                sampler_admin_pb2.ReadProviderRequestRequest(session_id=request.session_id, request_id=dispatches[-1]),
                metadata=METADATA,
                timeout=10,
            ):
                if part.HasField("metadata"):
                    metadata = part.metadata
                else:
                    body.extend(part.body_chunk)
            assert metadata is not None
            assert metadata.url.endswith("/v1/chat/completions")
            assert json.loads(body) == continued


@pytest.mark.parametrize("automatic", [False, True])
async def test_chat_completions_compaction(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path, automatic: bool
) -> None:
    def completion(reply: str, *, tokens: int = 12) -> tuple[SseEvent, SseEvent, SseEvent]:
        chunk = {"id": f"chat-{reply}", "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": "stop"}]}
        usage = {"choices": [], "usage": {"total_tokens": tokens}}
        return (
            SseEvent("chunk", b"data: " + json.dumps(chunk).encode() + b"\n\n"),
            SseEvent("usage", b"data: " + json.dumps(usage).encode() + b"\n\n"),
            SseEvent("done", b"data: [DONE]\n\n"),
        )

    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.models["test-model"] = ModelConfig(
            connection="provider",
            provider_model="chat-test-model",
            reasoning_efforts={""},
            api="chat_completions",
            exec_encoding="json",
            chat_compaction=ChatCompactionConfig(
                max_tokens=8192,
                keep_recent_turns=1,
                auto_compact_tokens=256 if automatic else None,
                summary_prompt_template=None if automatic else "Write a work handoff. Keep {{.KeepRecentTurns}} turns.",
                replacement=None
                if automatic
                else ChatCompactionReplacement(role="user", template="<handoff>{{.Summary}}</handoff>"),
            ),
        )
        request = create_request(tmp_path)
        request.spec.reasoning_effort = ""
        old = "old source " * 300
        recent = "Keep this request verbatim."
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(stub, session_id=request.session_id, command_id="old", text=old, timeout_s=10)
            async with await endpoint.next() as exchange:
                first_request = exchange.request.body
                assert first_request["stream_options"] == {"include_usage": True}
                await exchange.send(*completion("old answer", tokens=500))
            assert (await turn_completed(stub, request.session_id, "old")).status == event_pb2.TURN_STATUS_COMPLETED

            await submit_input(stub, session_id=request.session_id, command_id="recent", text=recent, timeout_s=10)
            if not automatic:
                async with await endpoint.next() as exchange:
                    await exchange.send(*completion("recent answer"))
                assert (
                    await turn_completed(stub, request.session_id, "recent")
                ).status == event_pb2.TURN_STATUS_COMPLETED
                await stub.admin.Compact(
                    sampler_admin_pb2.CompactRequest(session_id=request.session_id, command_id="compact"),
                    metadata=METADATA,
                    timeout=10,
                )

            async with await endpoint.next() as exchange:
                frozen = exchange.request.body
                assert frozen["max_tokens"] == 8192
                assert frozen["tool_choice"] == "none"
                assert frozen["tools"] == first_request["tools"]
                messages = frozen["messages"]
                assert messages[: len(first_request["messages"])] == first_request["messages"]
                assert {"role": "assistant", "content": "old answer"} in messages
                assert {"role": "user", "content": recent} in messages
                assert messages[-1]["role"] == "user"
                if automatic:
                    assert "handoff" in messages[-1]["content"]
                else:
                    assert messages[-1]["content"] == "Write a work handoff. Keep 1 turns."
                if automatic:
                    await exchange.send(*completion("truncated summary")[:-1])
                else:
                    await exchange.send(*completion("retained summary"))
            if automatic:
                async with await endpoint.next() as exchange:
                    assert exchange.request.body == frozen
                    await exchange.send(*completion("retained summary"))
            else:
                async for attempt in AsyncRetrying(stop=stop_after_delay(10), wait=wait_fixed(0.05), reraise=True):
                    with attempt:
                        receipt = await stub.admin.GetCommand(
                            sampler_admin_pb2.GetCommandRequest(session_id=request.session_id, command_id="compact"),
                            metadata=METADATA,
                            timeout=10,
                        )
                        assert receipt.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED
                await submit_input(stub, session_id=request.session_id, command_id="after", text="Go on.", timeout_s=10)

            async with await endpoint.next() as exchange:
                continued = exchange.request.body
                assert continued["tools"] == first_request["tools"]
                assert "tool_choice" not in continued
                assert continued["messages"][0] == first_request["messages"][0]
                messages = continued["messages"]
                expected = (
                    {"role": "assistant", "content": "Summary of earlier conversation:\nretained summary"}
                    if automatic
                    else {"role": "user", "content": "<handoff>retained summary</handoff>"}
                )
                assert expected in messages
                assert all("sampler_compaction_summary" not in m for m in messages)
                assert {"role": "user", "content": recent} in messages
                assert not any("old source" in m["content"] or "truncated summary" in m["content"] for m in messages)
                await exchange.send(*completion("done"))

            command = "recent" if automatic else "after"
            assert (await turn_completed(stub, request.session_id, command)).status == event_pb2.TURN_STATUS_COMPLETED


@pytest.mark.parametrize("dialect", ["lite", "standard"])
async def test_json_exec_runs_shell_resumes_and_recovers(
    database_url: str,
    execution_target: str,
    codex_binary: str,
    code_mode_host: str,
    tmp_path: Path,
    dialect: Literal["lite", "standard"],
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config = config.model_copy(
            update={
                "models": {
                    "test-model": ModelConfig(
                        connection="provider",
                        provider_model="test-model",
                        reasoning_efforts={"", "high"},
                        api="responses",
                        responses=ResponsesConfig(dialect=dialect),
                        exec_encoding="json",
                    )
                }
            }
        )
        Image.new("RGB", (1, 1), (255, 0, 0)).save(tmp_path / "small.png")
        request = create_request(tmp_path)
        request.spec.reasoning_effort = ""
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="standard-json",
                text="Run a shell command.",
                timeout_s=10,
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
                await turn_completed(stub, request.session_id, "standard-json")
            ).status == event_pb2.TURN_STATUS_COMPLETED


async def test_notify_outputs_follow_the_step_that_emitted_them(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = create_request(tmp_path)
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="notify", text="Notify from cells.", timeout_s=10
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
            assert (await turn_completed(stub, request.session_id, "notify")).status == event_pb2.TURN_STATUS_COMPLETED


async def test_interrupt_discards_queued_notify_output(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = create_request(tmp_path)
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub,
                session_id=request.session_id,
                command_id="interrupted",
                text="Notify, then get interrupted.",
                timeout_s=10,
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
                    harness_host_pb2.InterruptTurnRequest(
                        session_id=request.session_id, command_id="interrupt", turn_id="interrupted"
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                assert (
                    await turn_completed(stub, request.session_id, "interrupted")
                ).status == event_pb2.TURN_STATUS_INTERRUPTED
            assert await store.queued_notifications(request.session_id) == []
        settled = [
            entry.event.boundary_record.direction
            for entry in await store.events(request.session_id, 0, limit=10000)
            if entry.event.boundary_record.boundary == "notification"
        ]
        assert settled == ["discarded"]


async def test_view_image_reads_executor_file_and_prepares_cell_image(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
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
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(config) as stub:
            request = create_request(tmp_path)
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="view-image", text="Look at the image.", timeout_s=10
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
            assert (await turn_completed(stub, SESSION_ID, "view-image")).status == event_pb2.TURN_STATUS_COMPLETED
            # Nested read evidence keeps digests, not the file's bytes.
            file_base64 = base64.b64encode(oversized.getvalue()).decode()
            assert not [
                intent for intent in await Store(engine).intents() if file_base64 in f"{intent.request}{intent.result}"
            ]


async def test_input_yields_a_running_cell_and_joins_the_turn(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = create_request(tmp_path)
        async with service(config) as stub:
            await stub.CreateSession(request, metadata=METADATA, timeout=10)
            await submit_input(
                stub, session_id=request.session_id, command_id="steered", text="Run a long cell.", timeout_s=10
            )
            async with await endpoint.next() as exchange:
                # The cell never settles and would hold its observation for its whole yield time.
                source = '// @exec: {"yield_time_ms": 300000}\nawait new Promise(() => {});'
                await exchange.send(
                    *response_stream([CustomToolCall("long", "exec", source)], model="test-model").events
                )
            await submit_input(
                stub, session_id=request.session_id, command_id="steer", text="Read this instead.", timeout_s=10
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
            assert (await turn_completed(stub, request.session_id, "steered")).status == event_pb2.TURN_STATUS_COMPLETED
            steer = await stub.admin.GetCommand(
                sampler_admin_pb2.GetCommandRequest(session_id=request.session_id, command_id="steer"),
                metadata=METADATA,
                timeout=10,
            )
            assert steer.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED


if __name__ == "__main__":
    pytest_bazel.main()

# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
