"""Disconnect a WebSocket client without restarting app-server or its native child.

Unlike stdio process restart, the child model exchange stays open across the disconnect.
Snapshots recover the disconnected interval; resuming the loaded child attaches the new client
without starting another model request. All wire traffic is synthetic and retained as JSONL.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from aiohttp import ClientSession, ClientWebSocketResponse

from agentplane.native.codex import scenarios
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import EFFORT, MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.async_process import AsyncNativeProcess, TextRecord


@dataclass
class Connection:
    socket: ClientWebSocketResponse
    logs: Path
    received: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, frame: dict[str, Any]) -> None:
        with (self.logs / "stdin.jsonl").open("a") as output:
            output.write(json.dumps(frame) + "\n")
        await self.socket.send_json(frame)

    async def read(self) -> dict[str, Any]:
        frame = await self.socket.receive_json()
        assert isinstance(frame, dict)
        with (self.logs / "stdout.jsonl").open("a") as output:
            output.write(json.dumps(frame) + "\n")
        self.received.append(frame)
        return frame

    async def rpc(self, method: str, **params: Any) -> dict[str, Any]:
        request_id = str(uuid4())
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            frame = await self.read()
            if frame.get("id") == request_id:
                assert "error" not in frame, frame
                result = frame["result"]
                assert isinstance(result, dict)
                return result

    async def initialize(self) -> None:
        self.logs.mkdir()
        await self.rpc(
            "initialize",
            clientInfo={"name": "child-reconnect-probe", "version": "0.1"},
            capabilities={"experimentalApi": True},
        )
        await self.send({"method": "initialized"})

    async def delta(self, child_id: str) -> dict[str, Any]:
        while True:
            frame = await self.read()
            if frame.get("method") == "item/agentMessage/delta" and frame["params"]["threadId"] == child_id:
                params = frame["params"]
                assert isinstance(params, dict)
                return params


async def _websocket_address(process: AsyncNativeProcess) -> str:
    # The pinned app-server prints the actual bound address in its startup banner.
    async with asyncio.timeout(10):
        while True:
            path = process.logs / "stderr.jsonl"
            if path.exists():
                text = "".join(TextRecord.model_validate_json(line).text for line in path.read_text().splitlines())
                if match := re.search(r"listening on:\s+(ws://127\.0\.0\.1:[0-9]+)\r?\n", text):
                    return match[1]
            if not process.alive():
                raise RuntimeError(f"app-server exited before publishing its address: {process.logs}")
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("v2", [False, True], ids=["v1", "v2"])
@pytest.mark.parametrize("attach", [False, True], ids=["root-only", "child-resume"])
async def test_live_child_survives_disconnect_and_history_joins_the_new_stream(
    codex: CodexHarness, openai_responses: OpenAIResponses, v2: bool, attach: bool
) -> None:
    namespace = "collaboration" if v2 else "multi_agent_v1"
    endpoint = f"{openai_responses.origin}/v1"
    address = "ws://127.0.0.1:0"
    command = scenarios.command(codex.binary, endpoint=endpoint, max_retries=scenarios.MAX_RETRIES)
    command[-1] = address
    async with (
        asyncio.timeout(90),
        AsyncNativeProcess(
            codex.logs,
            command,
            cwd=codex.workspace,
            environment={
                **codex.base_environment,
                "NO_COLOR": "1",
                **scenarios.environment(endpoint=endpoint, token="test-key", codex_home=str(codex.codex_home)),
            },
        ) as process,
        ClientSession() as client,
    ):
        address = await _websocket_address(process)
        async with client.ws_connect(address) as socket:
            initial = Connection(socket, codex.logs / "initial-peer")
            await initial.initialize()
            root = (
                await initial.rpc(
                    "thread/start",
                    cwd=str(codex.workspace),
                    model=MODEL,
                    approvalPolicy="never",
                    sandbox="danger-full-access",
                    ephemeral=False,
                    baseInstructions=scenarios.BASE_INSTRUCTIONS,
                    config={
                        "model_reasoning_effort": EFFORT,
                        "features.multi_agent": True,
                        "features.multi_agent_v2": v2,
                    },
                )
            )["thread"]
            root_id = root["id"]
            await initial.rpc(
                "turn/start", threadId=root_id, input=[{"type": "text", "text": "Delegate a reconnect probe."}]
            )
            async with await openai_responses.await_next_request() as exchange:
                assert namespace in exchange.request.tool_names
                arguments: dict[str, Any] = {"message": "CHILD_RECONNECT_PROBE"}
                if v2:
                    arguments.update(task_name="reconnect", fork_turns="none")
                else:
                    arguments["fork_context"] = False
                await exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.FunctionCall("spawn_reconnect", "spawn_agent", arguments, namespace=namespace)],
                        model=MODEL,
                    ).events
                )
            async with (
                await openai_responses.await_next_request() as first,
                await openai_responses.await_next_request() as second,
            ):
                parent, child = (
                    (first, second) if first.request.client_metadata.thread_id == root_id else (second, first)
                )
                child_id = child.request.client_metadata.thread_id
                assert child_id != root_id
                assert parent.request.function_call_outputs[-1].call_id == "spawn_reconnect"
                await child.send(
                    *responses_sse.response_stream(
                        [
                            responses_sse.Message("BEFORE_DISCONNECT"),
                            responses_sse.FunctionCall("shell_before", "exec_command", {"cmd": "printf BEFORE_OUTPUT"}),
                        ],
                        model=MODEL,
                    ).events
                )
                await child.close()
                async with await openai_responses.await_next_request() as continued:
                    assert continued.request.client_metadata.thread_id == child_id
                    assert "BEFORE_OUTPUT" in continued.request.function_call_outputs[-1].output
                    while True:
                        frame = await initial.read()
                        if frame.get("method") == "item/completed" and frame["params"]["item"]["id"] == "shell_before":
                            assert frame["params"]["threadId"] == child_id
                            break
                    before_ids = {
                        frame["params"]["item"]["id"]
                        for frame in initial.received
                        if frame.get("method") == "item/completed" and frame["params"]["threadId"] == child_id
                    }
                    await socket.close()
                    assert process.alive()
                    await continued.send(
                        *responses_sse.response_stream(
                            [
                                responses_sse.Message("DURING_DISCONNECT"),
                                responses_sse.FunctionCall(
                                    "shell_during", "exec_command", {"cmd": "printf GAP_OUTPUT"}
                                ),
                            ],
                            model=MODEL,
                        ).events
                    )
                    await continued.close()
                    async with (
                        await openai_responses.await_next_request() as after_gap,
                        client.ws_connect(address) as new_socket,
                    ):
                        assert after_gap.request.client_metadata.thread_id == child_id
                        assert "GAP_OUTPUT" in after_gap.request.function_call_outputs[-1].output
                        reconnected = Connection(new_socket, codex.logs / "reconnected-peer")
                        await reconnected.initialize()
                        await reconnected.rpc("thread/resume", threadId=root_id, excludeTurns=True)
                        snapshot = (await reconnected.rpc("thread/read", threadId=child_id, includeTurns=True))[
                            "thread"
                        ]
                        assert snapshot["status"]["type"] == "active"
                        assert snapshot["parentThreadId"] == root_id
                        recovered_ids = {item["id"] for item in snapshot["turns"][-1]["items"]}
                        assert before_ids < recovered_ids
                        assert "DURING_DISCONNECT" in json.dumps(snapshot["turns"])
                        assert "GAP_OUTPUT" in json.dumps(snapshot["turns"])
                        if attach:
                            attached = (await reconnected.rpc("thread/resume", threadId=child_id))["thread"]
                            assert attached["id"] == child_id
                        stream = responses_sse.response_stream([responses_sse.Message("AFTER_RECONNECT")], model=MODEL)
                        prefix = stream.through("response.output_text.delta")
                        await after_gap.send(*prefix.events)
                        if attach:
                            delta = await reconnected.delta(child_id)
                            assert delta["delta"] == "AFTER_RECONNECT"
                        else:
                            with pytest.raises(TimeoutError):
                                await asyncio.wait_for(reconnected.delta(child_id), timeout=0.25)
                        await after_gap.send(*stream.events[len(prefix.events) :])
                        await after_gap.close()
                        while True:
                            finished = (await reconnected.rpc("thread/read", threadId=child_id, includeTurns=True))[
                                "thread"
                            ]
                            if finished["status"]["type"] == "idle":
                                break
                            await asyncio.sleep(0.05)
                        assert finished["turns"][-1]["status"] == "completed"
                        items = finished["turns"][-1]["items"]
                        assert len({item["id"] for item in items}) == len(items)
                        assert recovered_ids < {item["id"] for item in items}
                        assert [item["text"] for item in items if item["type"] == "agentMessage"] == [
                            "BEFORE_DISCONNECT",
                            "DURING_DISCONNECT",
                            "AFTER_RECONNECT",
                        ]
                        live_items = [
                            frame["params"]["item"]
                            for frame in reconnected.received
                            if frame.get("method") == "item/completed" and frame["params"]["threadId"] == child_id
                        ]
                        assert not recovered_ids.intersection(item["id"] for item in live_items)
                        assert [item["text"] for item in live_items] == (["AFTER_RECONNECT"] if attach else [])
                        with pytest.raises(TimeoutError):
                            async with asyncio.timeout(0.25):
                                await openai_responses.await_next_request()
                        assert await process.crash() < 0
                        await parent.wait_client_closed()


if __name__ == "__main__":
    pytest_bazel.main()
