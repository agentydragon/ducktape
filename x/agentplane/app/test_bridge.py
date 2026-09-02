"""One browser-shaped script over the bridge against a local runner, run for both harnesses: open a
session, stream it, send an input while streaming, open a second tab on the same session, reconnect
from the last event id, shut down."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_bazel
import uvicorn
from google.protobuf.json_format import MessageToDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.conftest import RunnerHandle
from x.agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

SANDBOX = "bridge-test-sandbox"
SESSION = "bridge-1"
SESSIONS = f"/sandboxes/{SANDBOX}/sessions"
EVENTS = f"{SESSIONS}/{SESSION}/events"


@dataclass(frozen=True)
class SseMessage:
    event: str
    id: int | None
    data: dict[str, Any]


async def next_message(lines: AsyncIterator[str]) -> SseMessage:
    """The next SSE message; comments (keepalives) are skipped."""
    event, event_id, data = "", None, ""
    async for line in lines:
        if line == "":
            if event:
                return SseMessage(event, event_id, json.loads(data))
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(": ")
        match field:
            case "event":
                event = value
            case "id":
                event_id = int(value)
            case "data":
                data = value
    raise AssertionError("the stream ended without a message")


async def read_until(lines: AsyncIterator[str], key: str) -> list[SseMessage]:
    """Events up to and including the first whose payload carries `key`."""
    seen: list[SseMessage] = []
    while True:
        message = await next_message(lines)
        seen.append(message)
        if key in message.data:
            return seen


@pytest.fixture
async def app_url(runner: RunnerHandle, inventory: SandboxInventory) -> AsyncIterator[str]:
    """The app served by uvicorn, with the one test sandbox resolving to the local runner. The
    server is real because SSE needs a response that streams, which an in-process ASGI transport
    would buffer."""

    async def address_of(name: str) -> str:
        assert name == SANDBOX
        return runner.target

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    bridge = RunnerBridge(address_of=address_of)
    server = uvicorn.Server(
        uvicorn.Config(create_app(inventory, bridge), host="127.0.0.1", port=port, log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(OSError)
    ):
        with attempt:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving
        await bridge.close()


async def test_the_bridge_streams_a_turn_to_every_tab_and_resumes_from_the_last_event_id(
    app_url: str, model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60) as http:
        opened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
        assert opened.status_code == 201, opened.text
        assert opened.json()["harness"] == "HARNESS_STATE_RUNNING"
        assert [row["sessionId"] for row in (await http.get(SESSIONS)).json()] == [SESSION]

        async with http.stream("GET", EVENTS) as first_tab:
            first = first_tab.aiter_lines()
            assert (await next_message(first)).event == "attached"
            # A fresh Open would supersede this stream, so opening again while streaming is refused.
            reopened = await http.post(SESSIONS, json={"session_id": SESSION, "spec": MessageToDict(spec)})
            assert reopened.status_code == 409, reopened.text
            accepted = await http.post(
                f"{SESSIONS}/{SESSION}/inputs", json={"inputId": "input-1", "text": "Reply with exactly: BRIDGE_OK"}
            )
            assert accepted.status_code == 202, accepted.text
            model.reply(await model.request(), Text("BRIDGE_OK"))
            seen = await read_until(first, "turnCompleted")
            # Every runner event, in order, from the start of the session's log: replay and live alike.
            assert [message.id for message in seen] == list(range(1, len(seen) + 1))
            assert all(message.event == "event" for message in seen)
            assert seen[-1].data["turnCompleted"]["status"] == "TURN_STATUS_COMPLETED"
            assert any("native" in message.data for message in seen)
            completed = [message.data["itemCompleted"] for message in seen if "itemCompleted" in message.data]
            assert [item["text"] for item in completed] == ["BRIDGE_OK"]

            # A second tab on the same session loads the history the first one saw, then both follow
            # the next turn live; the runner sees one attachment, so the first tab is not superseded.
            async with http.stream("GET", EVENTS) as second_tab:
                second = second_tab.aiter_lines()
                assert (await next_message(second)).event == "attached"
                assert await read_until(second, "turnCompleted") == seen
                accepted = await http.post(
                    f"{SESSIONS}/{SESSION}/inputs",
                    json={"inputId": "input-2", "text": "Reply with exactly: BRIDGE_TWO"},
                )
                assert accepted.status_code == 202, accepted.text
                model.reply(await model.request(), Text("BRIDGE_TWO"))
                on_first, on_second = await asyncio.gather(
                    read_until(first, "turnCompleted"), read_until(second, "turnCompleted")
                )
                assert on_first == on_second
                assert on_first[0].id == len(seen) + 1
                assert [
                    message.data["itemCompleted"]["text"] for message in on_first if "itemCompleted" in message.data
                ] == ["BRIDGE_TWO"]

        # A browser reconnecting sends the last id it saw; the next event follows it without a gap.
        cut = seen[len(seen) // 2].id
        assert cut is not None
        async with http.stream("GET", EVENTS, headers={"Last-Event-ID": str(cut)}) as stream:
            lines = stream.aiter_lines()
            assert (await next_message(lines)).event == "attached"
            assert (await next_message(lines)).id == cut + 1

        stopped = await http.post(f"{SESSIONS}/{SESSION}/shutdown")
        assert stopped.status_code == 202, stopped.text
        (summary,) = (await http.get(SESSIONS)).json()
        assert summary["harness"] == "HARNESS_STATE_STOPPED"
    model.assert_quiescent()


async def test_the_bridge_reports_what_the_runner_refuses(app_url: str) -> None:
    async with httpx.AsyncClient(base_url=app_url, timeout=60) as http:
        unknown = await http.post(f"{SESSIONS}/never-opened/inputs", json={"inputId": "x", "text": "hello"})
        assert unknown.status_code == 409
        assert "does not exist" in unknown.json()["detail"]
        malformed = await http.post(
            SESSIONS, json={"session_id": "s", "spec": {"provider": "PROVIDER_CLAUDE", "nope": 1}}
        )
        assert malformed.status_code == 422


if __name__ == "__main__":
    pytest_bazel.main()
