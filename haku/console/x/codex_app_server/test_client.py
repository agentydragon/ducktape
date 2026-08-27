"""The Codex runtime client's promises over a scripted native frame channel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
import pytest_bazel

from haku.console.config import CodexReasoningEffort
from haku.console.x.codex_app_server.client import CodexAppServer, CodexAppServerError, CodexThread
from haku.runtime.x.bridge.client import RecordedFrame
from haku.runtime.x.bridge.protocol import HarnessFrame


class CountingSink:
    def __init__(self, *, replayed_runner_seqs: frozenset[int] = frozenset()):
        self.numbered: list[tuple[int, HarnessFrame]] = []
        self.runner_seqs: list[int | None] = []
        self._next = 1
        self._replayed = replayed_runner_seqs

    def _number(self, frame: HarnessFrame) -> int:
        seq, self._next = self._next, self._next + 3
        self.numbered.append((seq, frame))
        return seq

    async def sent(self, frame: HarnessFrame) -> int:
        return self._number(frame)

    async def received(self, frame: HarnessFrame) -> RecordedFrame:
        self.runner_seqs.append(frame.seq)
        return RecordedFrame(fresh=frame.seq not in self._replayed, frame_seq=self._number(frame))


class ScriptedChannel:
    def __init__(self):
        self.written: list[dict[str, Any]] = []
        self.closed = False
        self._inbound: asyncio.Queue[HarnessFrame | None] = asyncio.Queue()

    def deliver(self, frame: dict[str, Any] | None, *, seq: int | None = None) -> None:
        self._inbound.put_nowait(None if frame is None else HarnessFrame(frame=frame, seq=seq))

    async def connect(self) -> None:
        pass

    async def write(self, frame: HarnessFrame) -> None:
        self.written.append(frame.frame)

    async def read_messages(self) -> AsyncIterator[HarnessFrame]:
        while (message := await self._inbound.get()) is not None:
            yield message

    async def close(self) -> None:
        self.closed = True


async def _written(channel: ScriptedChannel, count: int) -> None:
    for _ in range(100):
        if len(channel.written) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} writes, got {len(channel.written)}")


def _response(request: dict[str, Any], result: Any) -> dict[str, Any]:
    return {"id": request["id"], "result": result}


async def _connect_new(cli: CodexAppServer, channel: ScriptedChannel) -> Mapping[str, Any]:
    connecting = asyncio.create_task(cli.connect())
    await _written(channel, 1)
    loaded = channel.written[0]
    assert loaded["method"] == "thread/loaded/list"
    channel.deliver({"id": loaded["id"], "error": {"code": -32000, "message": "Not initialized"}})

    await _written(channel, 2)
    initialize = channel.written[1]
    assert initialize["method"] == "initialize"
    channel.deliver(_response(initialize, {"userAgent": "codex_cli_rs/0.144.1"}))

    await _written(channel, 4)
    assert channel.written[2] == {"method": "initialized"}
    thread_start = channel.written[3]
    assert thread_start["method"] == "thread/start"
    channel.deliver(_response(thread_start, {"thread": {"id": "thread-1"}}))
    return await connecting


async def test_new_process_handshake_thread_configuration_and_prompt_are_exact() -> None:
    channel, sink = ScriptedChannel(), CountingSink()
    cli = CodexAppServer(
        channel, sink, CodexThread(cwd="/workspace", developer_instructions="you are Haku"), request_timeout=5
    )
    assert await _connect_new(cli, channel) == {"userAgent": "codex_cli_rs/0.144.1"}
    assert channel.written[3]["params"] == {
        "cwd": "/workspace",
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
        "ephemeral": True,
        "developerInstructions": "you are Haku",
    }

    querying = asyncio.create_task(cli.query("hello"))
    await _written(channel, 5)
    request = channel.written[4]
    channel.deliver(_response(request, {"turn": {"id": "turn-1"}}))
    prompt = await querying

    assert request == {
        "method": "turn/start",
        "id": request["id"],
        "params": {"threadId": "thread-1", "input": [{"type": "text", "text": "hello", "text_elements": []}]},
    }
    assert sink.numbered[-2][0] == prompt.frame_seq
    await cli.aclose()


def test_thread_start_params_carry_the_reasoning_effort_as_a_config_override() -> None:
    # thread/start has no dedicated effort param at 0.144.1, so the effort travels in the
    # `config` override map under the server's own `model_reasoning_effort` key.
    with_effort = CodexThread(cwd="/workspace", model="gpt-test", reasoning_effort=CodexReasoningEffort.LOW)
    assert with_effort.start_params()["config"] == {"model_reasoning_effort": "low"}
    # Absent means the provider/config default, so no override may be sent at all.
    assert "config" not in CodexThread(cwd="/workspace").start_params()


async def test_an_initialized_process_is_adopted_with_its_active_turn_without_a_second_handshake() -> None:
    channel = ScriptedChannel()
    cli = CodexAppServer(channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await _written(channel, 1)
    channel.deliver(_response(channel.written[0], {"data": ["thread-existing"], "nextCursor": None}))
    await _written(channel, 2)
    thread_read = channel.written[1]
    assert thread_read["params"] == {"threadId": "thread-existing", "includeTurns": True}
    channel.deliver(
        _response(
            thread_read,
            {
                "thread": {
                    "id": "thread-existing",
                    "turns": [
                        {"id": "turn-finished", "status": "completed", "items": []},
                        {"id": "turn-active", "status": "inProgress", "items": []},
                    ],
                }
            },
        )
    )

    assert await connecting == {"threadId": "thread-existing", "activeTurnId": "turn-active", "adopted": True}
    assert [frame["method"] for frame in channel.written] == ["thread/loaded/list", "thread/read"]

    interrupting = asyncio.create_task(cli.interrupt())
    await _written(channel, 3)
    interrupt = channel.written[2]
    assert interrupt["params"] == {"threadId": "thread-existing", "turnId": "turn-active"}
    channel.deliver(_response(interrupt, {}))
    await interrupting
    await cli.aclose()


async def test_notifications_are_delivered_but_responses_and_server_requests_are_plumbing() -> None:
    channel, sink = ScriptedChannel(), CountingSink()
    cli = CodexAppServer(channel, sink, CodexThread(cwd="/workspace"), request_timeout=5)
    await _connect_new(cli, channel)

    notification = {"method": "item/agentMessage/delta", "params": {"itemId": "item-1", "delta": "hi"}}
    channel.deliver(notification, seq=8)
    received = await anext(cli.frames())

    assert received.envelope.frame == notification
    assert received.frame_seq == sink.numbered[-1][0]
    assert sink.runner_seqs[-1] == 8
    await cli.aclose()


async def test_interrupt_uses_the_active_native_thread_and_turn() -> None:
    channel = ScriptedChannel()
    cli = CodexAppServer(channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=5)
    await _connect_new(cli, channel)
    querying = asyncio.create_task(cli.query("hello"))
    await _written(channel, 5)
    channel.deliver(_response(channel.written[4], {"turn": {"id": "turn-1"}}))
    await querying

    interrupting = asyncio.create_task(cli.interrupt())
    await _written(channel, 6)
    request = channel.written[5]
    channel.deliver(_response(request, {}))
    await interrupting

    assert request["method"] == "turn/interrupt"
    assert request["params"] == {"threadId": "thread-1", "turnId": "turn-1"}
    await cli.aclose()


async def test_an_unsupported_server_request_is_refused_instead_of_blocking_codex() -> None:
    channel = ScriptedChannel()
    cli = CodexAppServer(channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=5)
    await _connect_new(cli, channel)

    channel.deliver({"method": "item/commandExecution/requestApproval", "id": 91, "params": {}})
    await _written(channel, 5)

    assert channel.written[-1] == {
        "id": 91,
        "error": {"code": -32601, "message": "item/commandExecution/requestApproval is not supported by this client"},
    }
    await cli.aclose()


async def test_replayed_frames_are_recorded_but_not_delivered_twice() -> None:
    channel = ScriptedChannel()
    sink = CountingSink(replayed_runner_seqs=frozenset({7}))
    cli = CodexAppServer(channel, sink, CodexThread(cwd="/workspace"), request_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await _written(channel, 1)
    channel.deliver({"method": "item/agentMessage/delta", "params": {"itemId": "old", "delta": "old"}}, seq=7)
    channel.deliver(_response(channel.written[0], {"data": ["thread-existing"], "nextCursor": None}), seq=8)
    await _written(channel, 2)
    channel.deliver(_response(channel.written[1], {"thread": {"id": "thread-existing", "turns": []}}), seq=9)
    await connecting
    channel.deliver(None)

    assert [frame async for frame in cli.frames()] == []
    assert sink.runner_seqs == [7, 8, 9]
    await asyncio.wait_for(cli.wait_closed(), timeout=1)
    await cli.aclose()


async def test_request_errors_and_timeouts_surface_to_the_owner() -> None:
    channel = ScriptedChannel()
    cli = CodexAppServer(channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=5)
    connecting = asyncio.create_task(cli.connect())
    await _written(channel, 1)
    channel.deliver({"id": channel.written[0]["id"], "error": {"code": 7, "message": "nope"}})
    with pytest.raises(CodexAppServerError, match="nope"):
        await connecting
    await cli.aclose()

    timed_out = CodexAppServer(ScriptedChannel(), CountingSink(), CodexThread(cwd="/workspace"), request_timeout=0.01)
    with pytest.raises(CodexAppServerError, match="thread/loaded/list"):
        await timed_out.connect()
    await timed_out.aclose()


async def test_replacement_clients_namespace_requests_away_from_late_predecessor_responses() -> None:
    first_channel, second_channel = ScriptedChannel(), ScriptedChannel()
    first = CodexAppServer(first_channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=5)
    second = CodexAppServer(second_channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=5)
    first_connect = asyncio.create_task(first.connect())
    second_connect = asyncio.create_task(second.connect())
    await _written(first_channel, 1)
    await _written(second_channel, 1)

    assert first_channel.written[0]["id"] != second_channel.written[0]["id"]

    first_connect.cancel()
    second_connect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_connect
    with pytest.raises(asyncio.CancelledError):
        await second_connect
    await first.aclose()
    await second.aclose()


async def test_transport_closure_fails_an_outstanding_request_without_waiting_for_its_timeout() -> None:
    channel = ScriptedChannel()
    cli = CodexAppServer(channel, CountingSink(), CodexThread(cwd="/workspace"), request_timeout=60)
    connecting = asyncio.create_task(cli.connect())
    await _written(channel, 1)
    channel.deliver(None)

    with pytest.raises(CodexAppServerError, match="connection closed"):
        await asyncio.wait_for(connecting, timeout=1)
    await cli.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
