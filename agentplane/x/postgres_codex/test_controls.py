"""Real host/executor control paths with independent PostgreSQL checkpoints."""

import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from aiohttp import ClientSession
from sqlalchemy import select

from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, response_stream
from agentplane.x.postgres_codex.code_mode import code_mode
from agentplane.x.postgres_codex.control_testing import cli, options, seed
from agentplane.x.postgres_codex.executor import Executor
from agentplane.x.postgres_codex.sampler import advance
from agentplane.x.postgres_codex.store import Base, Intent, KnownFailureError, LiveCell, Store, UnknownEffectError
from agentplane.x.postgres_codex.testing import drop_completed_process_reply
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def test_model_exec_yields_and_model_wait_observes_completion(
    database_url: str, sampler_binary: str, codex_binary: str, code_mode_host: str, executor_url: str, tmp_path: Path
) -> None:
    conversation_id = UUID(await cli(sampler_binary, database_url, "init", "yield and wait"))
    async with OpenAIResponses() as endpoint:
        task = asyncio.create_task(
            cli(
                sampler_binary,
                database_url,
                *options(
                    "run",
                    conversation_id,
                    executor_url,
                    codex_binary,
                    code_mode_host,
                    tmp_path,
                    endpoint.origin + "/v1/responses",
                ),
            )
        )
        async with await endpoint.await_next_request() as exchange:
            await exchange.send(
                *response_stream(
                    [
                        CustomToolCall(
                            "yielding-js",
                            "exec",
                            '// @exec: {"yield_time_ms": 1}\ntext((await tools.exec_command({cmd: "sleep 0.2; printf waited-cell"})).output);',
                        )
                    ],
                    model="test-model",
                ).events
            )
        async with await endpoint.await_next_request() as exchange:
            assert exchange.request.code_mode
            output = str(exchange.request.custom_tool_call_outputs[0].output)
            match = re.search(r"cell ID ([A-Za-z0-9_:-]+)", output)
            assert match is not None, output
            await exchange.send(
                *response_stream(
                    [CustomToolCall("wait-js", "wait", json.dumps({"cell_id": match[1], "yield_time_ms": 1000}))],
                    model="test-model",
                ).events
            )
        async with await endpoint.await_next_request() as exchange:
            assert "waited-cell" in str(exchange.request.custom_tool_call_outputs[-1].output)
            await exchange.send(*response_stream([Message("DONE")], model="test-model").events)
        assert await task == "done"
    async with postgres_async_engine(database_url) as engine:
        store = Store(engine)
        owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
        assert await store.execution_state(owner) == {"processes": [], "cells": []}
        assert (await store.read(owner)).pending is None


async def test_process_handle_survives_completed_cell_and_sampler_restart(
    database_url: str, codex_binary: str, code_mode_host: str, executor_url: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        conversation_id, _ = await seed(
            store, 'text(await tools.exec_command({cmd: "read line; printf answer:%s \\"$line\\"", yield_time_ms: 1}));'
        )
        first = await store.claim(conversation_id, lease=timedelta(seconds=30))
        async with ClientSession() as http, http.ws_connect(executor_url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, first)
            await executor.initialize()
            async with code_mode(
                code_mode_host, undeclared_outputs_dir() / "restart-1.log", store, first, executor
            ) as runtime:
                assert (
                    await advance(
                        store, first, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                    )
                    == "cell"
                )
        state = await store.execution_state(first)
        assert not state["cells"]
        (process,) = state["processes"]
        assert process["status"] == "running"
        await store.release(first)
        second = await Store(engine).claim(conversation_id, lease=timedelta(seconds=30))
        call = {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "stdin-cell",
            "input": "text(await tools.write_stdin("
            + json.dumps({"session_id": process["session_id"], "chars": "hello\n", "yield_time_ms": 1000})
            + "));",
        }
        intent = await store.begin(second, {"kind": "sampling"})
        await store.finish(second, intent, {"output": [call]}, [call])
        async with ClientSession() as http, http.ws_connect(executor_url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, second)
            await executor.initialize()
            async with code_mode(
                code_mode_host, undeclared_outputs_dir() / "restart-2.log", store, second, executor
            ) as runtime:
                assert (
                    await advance(
                        store, second, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                    )
                    == "cell"
                )
        assert "answer:hello" in str((await store.read(second)).items[-1])
        assert not (await store.execution_state(second))["processes"]
        async with store.sessions() as db:
            tools = (await db.scalars(select(Intent).where(Intent.request["kind"].astext == "tool"))).all()
            assert [t.request["tool_name"]["name"] for t in tools] == ["exec_command", "write_stdin"]
            assert all(t.result is not None for t in tools)


async def test_real_pty_eof_and_ctrl_c(database_url: str, codex_binary: str, executor_url: str, tmp_path: Path) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        owner = await store.claim(await store.create("terminal"), lease=timedelta(seconds=30))
        async with ClientSession() as http, http.ws_connect(executor_url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, owner)
            await executor.initialize()
            result = await executor.invoke("exec_command", {"cmd": "cat", "tty": True, "yield_time_ms": 1})
            session = result["session_id"]
            result = await executor.invoke(
                "write_stdin", {"session_id": session, "chars": "hello-eof\n", "yield_time_ms": 10}
            )
            assert "hello-eof" in result["output"]
            result = await executor.invoke("write_stdin", {"session_id": session, "eof": True, "yield_time_ms": 1000})
            assert result["exit_code"] == 0
            result = await executor.invoke("exec_command", {"cmd": "sleep 60", "yield_time_ms": 1})
            with pytest.raises(ValueError, match="EOF requires a PTY"):
                await executor.invoke("write_stdin", {"session_id": result["session_id"], "eof": True})
            result = await executor.invoke(
                "write_stdin", {"session_id": result["session_id"], "chars": "\x03", "yield_time_ms": 1000}
            )
            assert result["exit_code"] is not None
            assert not (await store.execution_state(owner))["processes"]


async def test_lost_yielded_host_is_not_reconstructed(
    database_url: str, codex_binary: str, code_mode_host: str, executor_url: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        conversation_id, _ = await seed(
            store, '// @exec: {"yield_time_ms": 1}\nawait new Promise(r => setTimeout(r, 10000)); text("no replay");'
        )
        owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
        async with ClientSession() as http, http.ws_connect(executor_url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, owner)
            await executor.initialize()
            async with code_mode(
                code_mode_host, undeclared_outputs_dir() / "lost-host.log", store, owner, executor
            ) as runtime:
                await advance(
                    store, owner, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                )
                with pytest.raises(KnownFailureError, match="known live cell_id"):
                    await runtime.execute({"name": "wait", "input": '{"cell_id":"missing"}'})
        assert (await store.read(owner)).pending is None
        await store.release(owner)
        successor = await store.claim(conversation_id, lease=timedelta(seconds=30))
        async with ClientSession() as http:
            with pytest.raises(UnknownEffectError, match="lost its host"):
                await advance(
                    store, successor, None, http, endpoint="http://unused.invalid", credential="test", model="test"
                )


async def test_lost_stdin_reply_blocks_replay_after_real_effect(
    database_url: str, codex_binary: str, code_mode_host: str, executor_url: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        conversation_id, _ = await seed(
            store,
            'text(await tools.exec_command({cmd: "read line; printf %s $line >> effect.txt", yield_time_ms: 1}));',
        )
        owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
        async with ClientSession() as http, http.ws_connect(executor_url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, owner)
            await executor.initialize()
            async with code_mode(
                code_mode_host, undeclared_outputs_dir() / "stdin-start.log", store, owner, executor
            ) as runtime:
                await advance(
                    store, owner, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                )
        (process,) = (await store.execution_state(owner))["processes"]
        await store.release(owner)
        owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
        call = {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "lost-stdin-cell",
            "input": "text(await tools.write_stdin("
            + json.dumps({"session_id": process["session_id"], "chars": "once\n"})
            + "));",
        }
        intent = await store.begin(owner, {"kind": "sampling"})
        await store.finish(owner, intent, {"output": [call]}, [call])
        async with (
            drop_completed_process_reply(executor_url) as (proxy, dropped),
            ClientSession() as http,
            http.ws_connect(proxy) as socket,
        ):
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, owner)
            await executor.initialize()
            async with code_mode(
                code_mode_host, undeclared_outputs_dir() / "stdin-lost.log", store, owner, executor
            ) as runtime:
                with pytest.raises(ConnectionError, match="remote effects may have occurred"):
                    await advance(
                        store, owner, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                    )
            assert dropped.is_set()
        assert (tmp_path / "effect.txt").read_text() == "once"
        await store.release(owner)
        successor = await store.claim(conversation_id, lease=timedelta(seconds=30))
        async with ClientSession() as http:
            with pytest.raises(UnknownEffectError, match="unresolved cell"):
                await advance(
                    store, successor, None, http, endpoint="http://unused.invalid", credential="test", model="test"
                )
        async with store.sessions() as db:
            tools = (
                await db.scalars(select(Intent).where(Intent.request["kind"].astext == "tool", Intent.result.is_(None)))
            ).all()
            assert len(tools) == 1
            assert tools[0].request["tool_name"]["name"] == "write_stdin"
        assert (tmp_path / "effect.txt").read_text() == "once"


async def test_wait_terminate_is_terminal_and_repeated_wait_fails_safely(
    database_url: str, codex_binary: str, code_mode_host: str, executor_url: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        conversation_id, _ = await seed(
            store, '// @exec: {"yield_time_ms": 1}\nawait new Promise(r => setTimeout(r, 10000));'
        )
        owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
        async with ClientSession() as http, http.ws_connect(executor_url) as socket:
            executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
            executor.bind(store, owner)
            await executor.initialize()
            async with code_mode(
                code_mode_host, undeclared_outputs_dir() / "terminate-wait.log", store, owner, executor
            ) as runtime:
                await advance(
                    store, owner, runtime, http, endpoint="http://unused.invalid", credential="test", model="test"
                )
                (cell,) = (await store.execution_state(owner))["cells"]
                output = await runtime.execute(
                    {"name": "wait", "input": json.dumps({"cell_id": cell["cell_id"], "terminate": True})}
                )
                assert "Cell terminated" in str(output)
                with pytest.raises(KnownFailureError, match="known live cell_id"):
                    await runtime.execute({"name": "wait", "input": json.dumps({"cell_id": cell["cell_id"]})})
                assert not (await store.execution_state(owner))["cells"]


async def test_cell_handles_isolate_conversations_and_fresh_hosts(
    database_url: str, codex_binary: str, code_mode_host: str, executor_url: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        store = Store(engine)
        first, _ = await seed(store, 'text("first");')
        second, _ = await seed(store, 'text("second");')
        async with ClientSession() as http:
            for conversation_id in (first, second, first):
                owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
                if conversation_id == first and owner.generation > 2:
                    call = {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "fresh-host-call",
                        "input": 'text("fresh");',
                    }
                    intent = await store.begin(owner, {"kind": "sampling"})
                    await store.finish(owner, intent, {"output": [call]}, [call])
                async with http.ws_connect(executor_url) as socket:
                    executor = Executor(socket, cwd=tmp_path, codex_path=codex_binary)
                    executor.bind(store, owner)
                    await executor.initialize()
                    async with code_mode(
                        code_mode_host, undeclared_outputs_dir() / "cell-identities.log", store, owner, executor
                    ) as runtime:
                        await advance(
                            store,
                            owner,
                            runtime,
                            http,
                            endpoint="http://unused.invalid",
                            credential="test",
                            model="test",
                        )
                await store.release(owner)
        async with store.sessions() as db:
            cells = (await db.scalars(select(LiveCell))).all()
            assert len(cells) == 3
            assert len({cell.id for cell in cells}) == 3
            assert all(cell.id.endswith(":1") for cell in cells)
            assert {cell.conversation_id for cell in cells} == {first, second}


if __name__ == "__main__":
    pytest_bazel.main()
