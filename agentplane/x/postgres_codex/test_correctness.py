"""Standalone failure settlement and unread process-output durability."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_bazel
from aiohttp import ClientSession
from sqlalchemy import select

from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.native.testing.model_endpoint import JsonResponse, SseEvent
from agentplane.x.postgres_codex.code_mode import code_mode
from agentplane.x.postgres_codex.executor import Executor
from agentplane.x.postgres_codex.sampler import advance
from agentplane.x.postgres_codex.store import Base, Intent, KnownFailureError, Owner, Store, UnknownEffectError
from agentplane.x.postgres_codex.testing import ExecutorServer
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture
async def owned_store(database_url: str) -> AsyncIterator[tuple[Store, Owner]]:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        conversation_id = await store.create("Start")
        yield store, await store.claim(conversation_id, lease=timedelta(seconds=60))


@pytest.mark.parametrize("failure", ["429", "503", "failed", "incomplete", "error", "unsupported", "partial"])
async def test_provider_failure_settlement(owned_store: tuple[Store, Owner], failure: str) -> None:
    store, owner = owned_store
    original = (await store.read(owner)).items
    async with OpenAIResponses() as endpoint, ClientSession() as http:
        endpoint_url = endpoint.origin + "/v1/responses"
        task = asyncio.create_task(
            advance(store, owner, None, http, endpoint=endpoint_url, credential="test-key", model="test-model")
        )
        async with await endpoint.await_next_request() as exchange:
            if failure in {"429", "503"}:
                await exchange.respond(JsonResponse(b'{"error":{"code":"provider_rejected"}}', int(failure)))
            elif failure in {"failed", "incomplete", "error"}:
                kind = "error" if failure == "error" else "response." + failure
                payload = {"type": kind, "response": {"status": failure, "error": {"code": "provider_rejected"}}}
                await exchange.send(SseEvent(kind, ("data: " + json.dumps(payload) + "\n\n").encode()))
            elif failure == "unsupported":
                await exchange.send(
                    *response_stream([CustomToolCall("unsupported", "invalid", "text(1)")], model="test-model").events
                )
            else:
                await exchange.send(
                    *response_stream([Message("Partial")], model="test-model")
                    .through("response.output_text.delta")
                    .events
                )
        with pytest.raises(ConnectionError if failure == "partial" else KnownFailureError):
            await task
        row = await store.read(owner)
        assert row.items == original
        async with store.sessions() as db:
            intent = (await db.scalars(select(Intent))).one()
            if failure == "partial":
                assert row.pending is not None
                assert intent.result is None
                with pytest.raises(UnknownEffectError, match="unresolved"):
                    await store.append_input(owner, "Continue")
                return
            assert row.pending is None
            assert row.stopped
            assert intent.result is not None
            assert intent.result["status"] == "rejected"
            if failure in {"429", "503"}:
                assert intent.result["http_status"] == int(failure)
            elif failure in {"failed", "incomplete", "error"}:
                assert intent.result["terminal"]["response"]["error"]["code"] == "provider_rejected"
        assert (
            await advance(store, owner, None, http, endpoint=endpoint_url, credential="test-key", model="test-model")
            == "interrupted"
        )
        await store.append_input(owner, "Continue")
        task = asyncio.create_task(
            advance(store, owner, None, http, endpoint=endpoint_url, credential="test-key", model="test-model")
        )
        async with await endpoint.await_next_request() as exchange:
            await exchange.send(*response_stream([Message("Recovered")], model="test-model").events)
        assert await task == "sample"
        assert (await store.read(owner)).items[-1]["content"][0]["text"] == "Recovered"


async def test_invalid_outer_tool_closes_undispatched_siblings(
    owned_store: tuple[Store, Owner], code_mode_host: str, codex_binary: str, executor_url: str, tmp_path: Path
) -> None:
    store, owner = owned_store
    calls = [
        {"type": "custom_tool_call", "call_id": "invalid", "name": "wait", "input": '{"cell_id":"missing"}'},
        {
            "type": "custom_tool_call",
            "call_id": "sibling",
            "name": "exec",
            "input": 'text(await tools.exec_command({cmd:"touch unwanted"}));',
        },
    ]
    intent_id = await store.begin(owner, {"kind": "sampling"})
    await store.finish(owner, intent_id, {"output": calls}, calls)
    async with ClientSession() as http, http.ws_connect(executor_url) as socket:
        executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
        executor.bind(store, owner)
        await executor.initialize()
        async with code_mode(
            code_mode_host, undeclared_outputs_dir() / "invalid-tool.log", store, owner, executor
        ) as runtime:
            with pytest.raises(KnownFailureError):
                await advance(
                    store, owner, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                )
    row = await store.read(owner)
    assert row.pending is None
    assert row.stopped
    outputs = [item for item in row.items if item["type"] == "custom_tool_call_output"]
    assert {item["call_id"] for item in outputs} == {"invalid", "sibling"}
    assert not (tmp_path / "unwanted").exists()
    await store.append_input(owner, "Continue")
    assert not (await store.read(owner)).stopped


@pytest.mark.parametrize("uncertainty", ["nested", "cell", "process"])
async def test_known_rejection_preserves_execution_uncertainty(
    owned_store: tuple[Store, Owner], uncertainty: str
) -> None:
    store, owner = owned_store
    call = {"type": "custom_tool_call", "call_id": "dispatched", "name": "exec", "input": "text(1)"}
    previous = await store.begin(owner, {"kind": "sampling"})
    await store.finish(owner, previous, {"output": [call]}, [call])
    if uncertainty == "nested":
        await store.record_nested(owner, {"kind": "tool", "name": "exec_command"})
    elif uncertainty == "cell":
        await store.cell_result(owner, "live-cell", "live")
    else:
        await store.register_process(owner, "live-process", tty=False)
    intent_id = await store.begin(owner, {"kind": "sampling"})
    await store.reject(owner, intent_id, KnownFailureError({"http_status": 429}))
    row = await store.read(owner)
    assert row.pending is not None
    assert row.items[-1] == call
    async with store.sessions() as db:
        rejected = await db.get(Intent, intent_id)
        assert rejected is not None
        assert rejected.result == {"status": "rejected", "http_status": 429, "not_dispatched": []}
    with pytest.raises(UnknownEffectError, match="unresolved"):
        await store.append_input(owner, "Continue")


async def test_unread_output_survives_owner_and_executor_session(
    owned_store: tuple[Store, Owner], codex_binary: str, executor_server: ExecutorServer, tmp_path: Path
) -> None:
    store, owner = owned_store
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    async with ClientSession() as http:
        async with http.ws_connect(executor_server.url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, owner)
            await executor.initialize()
            original_session = (await store.read(owner)).executor_session
            first = await executor.invoke(
                "exec_command", {"cmd": "printf before; cat gate; printf after", "yield_time_ms": 0}
            )
            process_id = first["session_id"]
            await asyncio.to_thread(gate.write_text, "middle")
            async with asyncio.timeout(10):
                while (await store.process(owner, process_id)).status != "exited_unread":
                    await executor.reconcile_processes(rpc_timeout_seconds=2)
            retained = await store.process(owner, process_id)
            assert retained.exit_code == 0
            assert first["output"] + retained.buffered_output.decode() == "beforemiddleafter"
            with pytest.raises(ValueError, match="stdin was not sent"):
                await executor.write_stdin({"session_id": process_id, "chars": "extra"})
        await store.release(owner)
        successor = await store.claim(owner.conversation_id, lease=timedelta(seconds=60))
        async with http.ws_connect(executor_server.url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, successor)
            await executor.initialize()
            assert (await store.read(successor)).executor_session != original_session
            executor_server.process.kill()
            await executor_server.process.wait()
            result = await executor.write_stdin({"session_id": process_id})
            assert first["output"] + result["output"] == "beforemiddleafter"
            assert result["exit_code"] == 0
            assert "session_id" not in result
            assert not (await store.execution_state(successor))["processes"]
            assert (await store.process(successor, process_id)).buffered_output == b""


async def test_unread_overflow_preserves_cursor(owned_store: tuple[Store, Owner]) -> None:
    store, owner = owned_store
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


if __name__ == "__main__":
    pytest_bazel.main()
