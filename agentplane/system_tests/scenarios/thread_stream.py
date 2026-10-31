"""What a browser sees of a Thread on a deployment: its tabs, its reconnects, and its commands.

Every tab follows the same archived log, keyed by event id; a reconnect resumes after its
`Last-Event-ID`; a command id answers only the command it first named; a turn nobody watches is
archived all the same, a failed one included, and the next input starts a turn of its own.

The app streams a Thread from History's archive alone. A deployment's History settles an item's
streamed deltas once its completion is archived, so a tab that reads later may skip cursors an
earlier one saw: two reads agree on every cursor both hold and on every entry settlement keeps.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.app.testing.models import SandboxView
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.system_tests.agent import RUNNER_ANSWERS_SECONDS, TURN_SECONDS, Agent
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import tracer
from agentplane.testing.scripted_model import Text

# What History's settlement may drop once a field completes (`history_service/storage/settlement.go`):
# its streamed deltas, each a native frame paired with the delta derived from it.
_SETTLEABLE = frozenset({"native", "textDelta", "toolArgumentsDelta", "toolOutputDelta"})


@dataclass(frozen=True)
class Frame:
    event: str
    id: int | None
    data: dict[str, Any]


class Tab:
    """One browser tab's `EventSource` on a Thread: the frames as a browser parses them."""

    def __init__(self, lines: AsyncIterator[str]) -> None:
        self._lines = lines

    async def next(self) -> Frame:
        """The next frame; keepalive comments are skipped."""
        event, event_id, data = "", None, ""
        async for line in self._lines:
            if line == "":
                if event:
                    return Frame(event, event_id, json.loads(data))
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
        raise AssertionError("the stream closed without a frame")

    async def until(self, observation: str) -> list[Frame]:
        """Event frames up to and including the first carrying `observation`; the stream may not end first."""
        with tracer.start_as_current_span(
            "system_test.thread_stream.event_wait", record_exception=False, set_status_on_exception=False
        ):
            seen: list[Frame] = []
            while True:
                frame = await self.next()
                if frame.event != "event":
                    observed = [list(row.data["event"]) for row in seen[-20:]]
                    raise AssertionError(f"{frame.event} frame before {observation}: {frame.data=} {observed=}")
                seen.append(frame)
                if observation in frame.data["event"]:
                    return seen


@asynccontextmanager
async def tab(http: httpx.AsyncClient, thread_id: UUID, *, last_event_id: int | None = None) -> AsyncIterator[Tab]:
    headers = {"Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    async with asyncio.timeout(TURN_SECONDS):
        with tracer.start_as_current_span(
            "system_test.thread_stream.tab_connection", record_exception=False, set_status_on_exception=False
        ):
            async with http.stream("GET", f"/threads/{thread_id}/events/stream", headers=headers) as response:
                assert response.status_code == 200, await response.aread()
                yield Tab(response.aiter_lines())


async def _open(client: Client, view: SandboxView, harness: runner_pb2.Harness, model: str) -> UUID:
    """A Thread whose runner feed is attached, so a tab's first frame is `attached`."""
    thread_id = (await Agent.open(client, sandbox=view.name, harness=harness, model=model)).thread_id
    with tracer.start_as_current_span(
        "system_test.thread_stream.feed_ready", record_exception=False, set_status_on_exception=False
    ):
        await client.wait_for_thread_feed(thread_id, timeout_s=RUNNER_ANSWERS_SECONDS)
    return thread_id


def _input(label: str, text: str) -> command_pb2.Command:
    # Command IDs are deployment-wide. Keep retries on the returned command while
    # giving concurrent harness cases distinct identities.
    return command_pb2.Command(command_id=f"{label}-{uuid4()}", submit_input=command_pb2.SubmitInput(text=text))


async def _submit(client: Client, thread_id: UUID, command: command_pb2.Command) -> None:
    """Submit `command`; the answer, the Dispatcher's or the archive's, names that same command."""
    with tracer.start_as_current_span(
        "system_test.thread_stream.submit", record_exception=False, set_status_on_exception=False
    ):
        answer = await client.submit_input(thread_id, command.command_id, command.submit_input.text)
    if isinstance(answer, session_dispatcher_pb2.Dispatch):
        assert (answer.command_id, answer.operation) == (
            command.command_id,
            session_dispatcher_pb2.OPERATION_SUBMIT_INPUT,
        )
    else:
        assert isinstance(answer, event_log_pb2.EventEntry)
        assert answer.event.command_admitted.command == command


async def _turn_status(client: Client, thread_id: UUID, status: str) -> None:
    """Wait for the Thread's summary to fold its latest turn to `status`."""
    with tracer.start_as_current_span(
        "system_test.thread_stream.turn_status", record_exception=False, set_status_on_exception=False
    ):
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(REQUEST_SECONDS), wait=wait_fixed(0.2), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                assert (await client.thread(thread_id)).last_turn_status == status


def _cursor(frame: Frame) -> int:
    return int(frame.data["cursor"])


def _ordered(frames: list[Frame]) -> None:
    """Each frame's id is its entry's cursor, and they only rise."""
    cursors = [_cursor(frame) for frame in frames]
    assert [frame.id for frame in frames] == cursors
    assert cursors == sorted(set(cursors)), cursors


def _same_log(one: list[Frame], other: list[Frame]) -> None:
    """Two reads of one archive agree on every cursor both hold and on every entry settlement keeps."""
    held = {frame.id: frame.data for frame in other}
    assert [frame.data for frame in one if frame.id in held] == [held[frame.id] for frame in one if frame.id in held]
    assert _retained(one) == _retained(other)


def _settleable(frame: Frame) -> bool:
    return bool(_SETTLEABLE & frame.data["event"].keys())


def _retained(frames: list[Frame]) -> list[dict[str, Any]]:
    return [frame.data for frame in frames if not _settleable(frame)]


def _texts(frames: list[Frame]) -> list[str]:
    return [frame.data["event"]["itemCompleted"]["text"] for frame in frames if "itemCompleted" in frame.data["event"]]


def _admitted(frames: list[Frame]) -> list[str]:
    return [
        frame.data["event"]["commandAdmitted"]["command"]["commandId"]
        for frame in frames
        if "commandAdmitted" in frame.data["event"]
    ]


async def an_unknown_thread_is_not_found(http: httpx.AsyncClient) -> None:
    unknown = "/threads/00000000-0000-0000-0000-000000000000"
    assert (await http.get(f"{unknown}/events/stream")).status_code == 404
    assert (
        await http.post(f"{unknown}/submit-input", json={"commandId": "test-x", "text": "hello"})
    ).status_code == 404
    assert (await http.post(f"{unknown}/submit-input", json={"commandId": "test-x"})).status_code == 422


async def every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event(
    client: Client,
    http: httpx.AsyncClient,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    view = await sandbox(f"systest-tabs-{harness}")
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)
    thread_id = await _open(client, view, harness, offered_model)

    async with tab(http, thread_id) as first:
        assert (await first.next()).event == "attached"
        command = _input("test-tabs-first", "Reply with exactly: TABS_FIRST")
        await _submit(client, thread_id, command)
        # The id names that command alone, whether the Dispatcher or the archive answers.
        reused = await http.post(
            f"/threads/{thread_id}/submit-input", json={"commandId": command.command_id, "text": "Something else"}
        )
        assert reused.status_code == 409, reused.text
        # An exact retry, as after a lost response, answers the same command.
        await _submit(client, thread_id, command)
        request = await model.request()
        assert request.user_texts[-1] == command.submit_input.text
        await model.reply(request, Text("TABS_FIRST"))
        seen = await first.until("turnCompleted")
        _ordered(seen)
        assert seen[-1].data["event"]["turnCompleted"]["status"] == "TURN_STATUS_COMPLETED"
        assert _texts(seen) == ["TABS_FIRST"]
        assert _admitted(seen) == [command.command_id]
        assert all(frame.data["origin"]["sourceId"] for frame in seen)
        assert any("native" in frame.data["event"] for frame in seen)

        # A second tab replays the archive, then both follow the same live log.
        async with tab(http, thread_id) as second:
            assert (await second.next()).event == "attached"
            _same_log(seen, await second.until("turnCompleted"))
            command = _input("test-tabs-second", "Reply with exactly: TABS_SECOND")
            await _submit(client, thread_id, command)
            request = await model.request()
            assert request.user_texts[-1] == command.submit_input.text
            await model.reply(request, Text("TABS_SECOND"))
            on_first, on_second = await asyncio.gather(first.until("turnCompleted"), second.until("turnCompleted"))
            _ordered(on_first)
            _same_log(on_first, on_second)
            assert _cursor(on_first[0]) > _cursor(seen[-1])
            assert _texts(on_first) == ["TABS_SECOND"]

    async with tab(http, thread_id) as replay:
        await replay.next()
        await replay.until("turnCompleted")
        archived = await replay.until("turnCompleted")
    # A reconnecting browser sends the last id it saw, and the stream resumes right after it. Two
    # adjacent entries settlement cannot drop stay adjacent through any settlement.
    cut, following = next(
        (one, other) for one, other in itertools.pairwise(archived) if not (_settleable(one) or _settleable(other))
    )
    async with tab(http, thread_id, last_event_id=_cursor(cut)) as reconnected:
        assert (await reconnected.next()).event == "attached"
        assert await reconnected.next() == following


async def turns_nobody_watches_are_archived_and_a_failed_one_leaves_the_next_input_its_own_turn(
    client: Client,
    http: httpx.AsyncClient,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    offered_model: str,
    scripted: ScriptedUpstream,
) -> None:
    """No tab is open while either turn runs: the runner's feed, not a watcher, is what archives them."""
    view = await sandbox(f"systest-unwatched-{harness}")
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)
    thread_id = await _open(client, view, harness, offered_model)

    failing = _input("test-failing-input", "Reply with exactly: MODEL_HTTP_ERROR")
    await _submit(client, thread_id, failing)
    # The runner allows each harness two retries, so the third error fails the turn.
    for _ in range(3):
        request = await model.request()
        assert request.user_texts[-1] == failing.submit_input.text
        await model.http_error(request)
    await _turn_status(client, thread_id, "TURN_STATUS_FAILED")
    async with tab(http, thread_id) as reader:
        await reader.next()
        failed = await reader.until("turnCompleted")
    _ordered(failed)
    assert failed[-1].data["event"]["turnCompleted"]["status"] == "TURN_STATUS_FAILED"
    assert _admitted(failed) == [failing.command_id]
    assert any("native" in frame.data["event"] for frame in failed)
    # Reading it again, as a reload does, shows the same failed turn.
    async with tab(http, thread_id) as reloaded:
        await reloaded.next()
        _same_log(failed, await reloaded.until("turnCompleted"))

    follow_up = _input("test-follow-up-input", "Reply with exactly: AFTER_HTTP_ERROR_OK")
    await _submit(client, thread_id, follow_up)
    request = await model.request()
    assert request.user_texts[-1] == follow_up.submit_input.text
    await model.reply(request, Text("AFTER_HTTP_ERROR_OK"))
    await _turn_status(client, thread_id, "TURN_STATUS_COMPLETED")
    async with tab(http, thread_id, last_event_id=_cursor(failed[-1])) as reader:
        await reader.next()
        recovered = await reader.until("turnCompleted")
    assert recovered[-1].data["event"]["turnCompleted"]["status"] == "TURN_STATUS_COMPLETED"
    assert recovered[-1].data["event"]["turnCompleted"]["turnId"] != failed[-1].data["event"]["turnCompleted"]["turnId"]
    assert _admitted(recovered) == [follow_up.command_id]
    assert _texts(recovered) == ["AFTER_HTTP_ERROR_OK"]


# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
