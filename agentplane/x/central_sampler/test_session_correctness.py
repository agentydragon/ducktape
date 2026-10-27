"""Failure and interrupt settlement preserve safe subsequent turns."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_bazel
from sqlalchemy import select, update

from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.proto import command_pb2, event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.migration import runner
from agentplane.x.central_sampler.store import PreconditionError, Store
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.postgres_codex.store import Conversation, Intent, InterruptRequest, KnownFailureError
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture
async def store(database_url: str) -> AsyncIterator[Store]:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(runner().run_for_connection)
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
        async with service(store, config) as stub:
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
                                CustomToolCall("bad-wait", "wait", '{"cell_id":"missing"}'),
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
            async with store.sessions() as db:
                intents = list(await db.scalars(select(Intent).order_by(Intent.id)))
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
                        item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"
                    ]
                    assert {item["call_id"] for item in outputs} == {"bad-wait", "sibling"}
                    assert not (tmp_path / "unwanted").exists()
                await exchange.send(*response_stream([Message("Recovered")], model="test-model").events)
            assert (await wait_turn(stub, request.session_id, "second")).status == event_pb2.TURN_STATUS_COMPLETED


@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_late_interrupt_is_settled_with_its_turn(
    store: Store, tmp_path: Path, takeover: bool, failure: bool
) -> None:
    session = await store.open_session(open_request(tmp_path), "test-token")
    owner = await store.claim(session.conversation_id, lease=timedelta(seconds=60))
    for command_id in ("first", "second"):
        await store.admit(
            session.id,
            command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=command_id)),
            "test-token",
        )
    command = await store.next_command(owner, session.id)
    assert command is not None
    assert command.id == "first"
    for command_id in ("interrupt-one", "interrupt-two"):
        await store.admit(
            session.id,
            command_pb2.Command(command_id=command_id, interrupt_turn=command_pb2.InterruptTurn(turn_id="first")),
            "test-token",
        )
    if takeover:
        async with store.sessions.begin() as db:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == session.conversation_id)
                .values(lease_until=Conversation.lease_until - timedelta(minutes=5))
            )
        owner = await store.claim(session.conversation_id, lease=timedelta(seconds=60))
        command = await store.next_command(owner, session.id)
        assert command is not None
        assert command.id == "first"
    assert await store.pending_interrupts(owner)
    if failure:
        await store.fail_execution(
            owner, session.id, "first", failure=KnownFailureError({"reason": "rejected before dispatch"})
        )
    else:
        await store.finish_command(owner, session.id, "first", "done")
    command = await store.next_command(owner, session.id)
    assert command is not None
    assert command.id == "second"
    assert await store.pending_interrupts(owner) == []
    async with store.sessions() as db:
        requests = list(await db.scalars(select(InterruptRequest)))
        assert requests
        assert all(request.target_turn == "first" and request.result is not None for request in requests)
    # The dispatch guard must agree with the interrupt watcher.
    await store.begin(owner, {"kind": "sampling"})
    for command_id in ("interrupt-one", "interrupt-two"):
        assert (await store.get_command(session.id, command_id)).outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED
    with pytest.raises(PreconditionError):
        await store.admit(
            session.id,
            command_pb2.Command(command_id="too-late", interrupt_turn=command_pb2.InterruptTurn(turn_id="first")),
            "test-token",
        )


@pytest.mark.parametrize("uncertainty", ["nested", "cell", "process"])
async def test_known_rejection_does_not_clear_other_effect_uncertainty(
    store: Store, tmp_path: Path, uncertainty: str
) -> None:
    session = await store.open_session(open_request(tmp_path), "test-token")
    owner = await store.claim(session.conversation_id, lease=timedelta(seconds=60))
    await store.admit(
        session.id,
        command_pb2.Command(command_id="first", submit_input=command_pb2.SubmitInput(text="Start")),
        "test-token",
    )
    await store.next_command(owner, session.id)
    if uncertainty == "nested":
        await store.record_nested(owner, {"kind": "tool", "name": "exec_command"})
    elif uncertainty == "cell":
        await store.cell_result(owner, "live-cell", "live")
    else:
        await store.register_process(owner, "live-process", tty=False)
    intent_id = await store.begin(owner, {"kind": "sampling"})
    await store.reject(owner, intent_id, KnownFailureError({"http_status": 429}))
    assert (await store.get_session(session.id)).blocked
    assert (await store.get_command(session.id, "first")).outcome == sampler_pb2.COMMAND_OUTCOME_UNKNOWN
    with pytest.raises(PreconditionError):
        await store.admit(
            session.id,
            command_pb2.Command(command_id="second", submit_input=command_pb2.SubmitInput(text="Continue")),
            "test-token",
        )


async def test_unread_output_overflow_preserves_the_durable_checkpoint(store: Store, tmp_path: Path) -> None:
    session = await store.open_session(open_request(tmp_path), "test-token")
    owner = await store.claim(session.conversation_id, lease=timedelta(seconds=60))
    await store.register_process(owner, "retained-process", tty=False)
    await store.process_result(owner, "retained-process", 0, "running")
    retained = b"x" * (1024 * 1024)
    await store.retain_process_output(owner, "retained-process", 1, retained, exited=False, exit_code=None)
    with pytest.raises(ValueError, match="cursor was not advanced"):
        await store.retain_process_output(owner, "retained-process", 2, b"overflow", exited=True, exit_code=0)
    process = await store.process(owner, "retained-process")
    assert process.after_seq == 1
    assert process.buffered_output == retained
    assert process.status == "running"
    await store.fail_execution(owner, session.id, None)
    assert (await store.get_session(session.id)).blocked


if __name__ == "__main__":
    pytest_bazel.main()

# Generated service stubs require their direct typing dependencies.
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf
