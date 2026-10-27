"""Standalone API with real Postgres, V8 host and executor; no Runner or History."""

import asyncio
import json
import re
import shlex
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_bazel
from sqlalchemy import select

from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.native.testing.model_endpoint import SseEvent
from agentplane.proto import command_pb2, event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.migration import runner
from agentplane.x.central_sampler.settings import Settings
from agentplane.x.central_sampler.store import Store
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.postgres_codex.store import Conversation, Intent, RemoteProcess
from agentplane.x.postgres_codex.testing import ExecutorServer
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


async def test_api_wait_stdin_queue_compaction_and_restart(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(store, config) as stub:
            opened = await stub.OpenSession(open_request(tmp_path), metadata=METADATA, timeout=10)
            assert opened.attached.session_id == "079bd706-77b3-4b71-8c7e-605a7e19324c"
            request = runner_service_pb2.SubmitInputRequest(
                session_id="079bd706-77b3-4b71-8c7e-605a7e19324c", command_id="first", text="Exercise execution. " * 100
            )
            first = await stub.SubmitInput(request, metadata=METADATA, timeout=10)
            assert await stub.SubmitInput(request, metadata=METADATA, timeout=10) == first
            async with await endpoint.next() as exchange:
                # Hold the provider while another command is durably admitted.
                queued = await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id="079bd706-77b3-4b71-8c7e-605a7e19324c",
                        command_id="queued",
                        text="Continue after the first turn.",
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                assert queued.admission.cursor > first.admission.cursor
                await exchange.send(
                    *response_stream(
                        [
                            CustomToolCall(
                                "start",
                                "exec",
                                'text(await tools.exec_command({cmd: "read line; printf received:%s \\"$line\\"", yield_time_ms: 1}));',
                            )
                        ],
                        model="test-model",
                    ).events
                )
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                process = json.loads(outputs[-1]["output"][0]["text"])["session_id"]
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
                            [CustomToolCall("wait", "wait", json.dumps({"cell_id": match[1], "yield_time_ms": 1000}))],
                            model="test-model",
                        ).events
                    )
                else:
                    pytest.fail("script did not yield a JS cell")
            async with await endpoint.next() as exchange:
                assert "received:hello" in json.dumps(exchange.request.body)
                await exchange.send(*response_stream([Message("first complete")], model="test-model").events)
            assert (await completed(stub, "first")).status == event_pb2.TURN_STATUS_COMPLETED
            async with await endpoint.next() as exchange:
                assert any(
                    item.get("content") == "Continue after the first turn." for item in exchange.request.body["input"]
                )
                await exchange.send(*response_stream([Message("second complete")], model="test-model").events)
            await completed(stub, "queued")
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
        async with service(Store(engine), config) as stub:
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
        async with store.sessions() as db:
            assert all(intent.result is not None for intent in await db.scalars(select(Intent)))


async def test_interrupt_streaming_evidence_and_uid_authorization(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(store, config) as stub:
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


async def test_restart_never_replays_unresolved_provider_intent(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with AsyncExitStack() as active:
            stub = await active.enter_async_context(service(store, config))
            request = open_request(tmp_path)
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            command = runner_service_pb2.SubmitInputRequest(
                session_id=request.session_id, command_id="lost", text="Provider outcome will be lost"
            )
            receipt = await stub.SubmitInput(command, metadata=METADATA, timeout=10)
            async with await endpoint.next():
                async with store.sessions() as db:
                    intent = (await db.scalars(select(Intent))).one()
                    assert intent.result is None
                    intent_id = intent.id
                await active.aclose()
        async with service(Store(engine), config) as stub:
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
        async with store.sessions() as db:
            intents = list(await db.scalars(select(Intent)))
            assert len(intents) == 1
            assert intents[0].id == intent_id
            assert intents[0].result is None


async def test_interrupt_admission_survives_unavailable_executor(
    database_url: str, executor_server: ExecutorServer, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_server.url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        async with service(store, config) as stub:
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
                                'text(await tools.exec_command({cmd: "read line", yield_time_ms: 1}));',
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
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        async with service(store, config) as stub:
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
        async with service(Store(engine), config) as stub:
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
        async with store.sessions() as db:
            intents = list(await db.scalars(select(Intent).where(Intent.request["kind"].astext == "cell")))
            assert len(intents) == 1


async def test_large_boundary_fragments_preserve_full_payload(database_url: str, tmp_path: Path) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        session = await store.open_session(open_request(tmp_path), "test-token")
        owner = await store.claim(session.conversation_id, lease=timedelta(seconds=60))
        payload = {"type": "response.output_text.delta", "item_id": "large", "delta": "λ" * (3 * 1024 * 1024)}
        await store.protocol_event(owner, "large-provider-record", "provider", "received", payload)
        entries = await store.events(session.id, 0, limit=100)
        fragments = [entry for entry in entries if entry.event.HasField("boundary_record")]
        assert len(fragments) > 4
        assert len({entry.event.boundary_record.record_id for entry in fragments}) == 1
        assert [entry.event.boundary_record.fragment_index for entry in fragments] == list(range(len(fragments)))
        assert all(entry.event.boundary_record.fragment_count == len(fragments) for entry in fragments)
        assert json.loads(b"".join(entry.event.boundary_record.payload for entry in fragments)) == payload
        assert all(entry.ByteSize() < 4 * 1024 * 1024 for entry in entries)
        derived = [entry for entry in entries if entry.event.source_sequences]
        assert len(derived) == 2
        assert all(
            list(entry.event.source_sequences) == [part.origin.sequence for part in fragments] for entry in derived
        )
        normalized = next(entry.event.text_delta.text for entry in entries if entry.event.HasField("text_delta"))
        assert "truncated" in normalized
        assert len(normalized.encode()) < 257 * 1024


async def test_recovery_selects_only_pending_work_with_bounded_results(database_url: str, tmp_path: Path) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        first = await store.open_session(open_request(tmp_path), "test-token")
        request = open_request(tmp_path)
        request.session_id = "11111111-1111-4111-8111-111111111111"
        second = await store.open_session(request, "test-token")
        assert await store.recoverable_sessions(exclude=set(), limit=2) == []
        for session in (first, second):
            await store.admit(
                session.id,
                command_pb2.Command(command_id="queued", submit_input=command_pb2.SubmitInput(text="Pending work")),
                "test-token",
            )
        assert await store.recoverable_sessions(exclude=set(), limit=1) == [first.id]
        assert await store.recoverable_sessions(exclude={first.id}, limit=1) == [second.id]
        assert await store.recoverable_sessions(exclude={first.id, second.id}, limit=2) == []
        owner = await store.claim(first.conversation_id, lease=timedelta(seconds=60))
        assert await store.recoverable_sessions(exclude=set(), limit=1) == [second.id]
        await store.release(owner)
        assert await store.recoverable_sessions(exclude=set(), limit=1) == [first.id]


async def test_unpolled_process_exit_releases_capacity_and_retains_output(
    database_url: str, executor_url: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
        store = Store(engine)
        config = settings(
            database_url, endpoint.origin, executor_url, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config.max_live_sessions = 1
        release = tmp_path / "release-process"
        request = open_request(tmp_path)
        second = open_request(tmp_path)
        second.session_id = "11111111-1111-4111-8111-111111111111"
        async with service(store, config) as stub:
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
                process_id = json.loads(outputs[-1]["output"][0]["text"])["session_id"]
                await exchange.send(*response_stream([Message("Process started.")], model="test-model").events)
            assert (await completed(stub, "background")).status == event_pb2.TURN_STATUS_COMPLETED
            async with store.sessions() as db:
                process = await db.get(RemoteProcess, process_id)
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
            async with store.sessions() as db:
                process = await db.get(RemoteProcess, process_id)
                assert process is not None
                assert process.status == "exited_unread"
                assert process.buffered_output == b"retained-output"
                conversation = await db.get(Conversation, process.conversation_id)
                assert conversation is not None
                assert conversation.owner is None
            await stub.SubmitInput(
                runner_service_pb2.SubmitInputRequest(
                    session_id=request.session_id, command_id="read-retained", text="Read the retained output."
                ),
                metadata=METADATA,
                timeout=10,
            )
            async with await endpoint.next() as exchange:
                assert process_id in json.dumps(exchange.request.body)
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
                assert json.loads(outputs[-1]["output"][0]["text"]) == {"output": "retained-output", "exit_code": 0}
                await exchange.send(*response_stream([Message("Retained output read.")], model="test-model").events)
            assert (await completed(stub, "read-retained")).status == event_pb2.TURN_STATUS_COMPLETED
            async with store.sessions() as db:
                process = await db.get(RemoteProcess, process_id)
                assert process is not None
                assert process.status == "exited"
                assert process.buffered_output == b""


def test_gateway_address_selects_the_executor_control_service(tmp_path: Path) -> None:
    config = settings("postgresql+asyncpg://test", "http://model", "ws://executor", "codex", "host", tmp_path)
    config = Settings.model_validate(
        config.model_dump()
        | {
            "direct_executor_url": None,
            "gateway_target": "{sandbox}-control.{namespace}.svc:8080",
            "gateway_token_file": tmp_path / "token",
        }
    )
    assert config.gateway_address("execution", "worker-1") == "worker-1-control.execution.svc:8080"
    with pytest.raises(ValueError, match="DNS-label"):
        config.gateway_address("elsewhere:1234/", "worker-1")
    with pytest.raises(ValueError, match="only supports"):
        Settings.model_validate(config.model_dump() | {"gateway_target": "{pod}.svc:8080"})


if __name__ == "__main__":
    pytest_bazel.main()

# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
