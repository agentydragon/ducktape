"""One agent session, driven to the end of a turn.

A scenario asks a real harness to do something and then checks what the system recorded, never what
the agent said about it: the model's prose is not a contract, and a scenario that greps it fails on
a paraphrase. What the agent produced is collected all the same, because it is what makes a failure
readable.

Which harness runs is the caller's, not this module's: the runner protocol is the same for both, so
a scenario parametrised over `Harness` gets Claude and Codex from one body.
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, TypeVar
from uuid import UUID, uuid4

import httpx
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel
from tenacity import AsyncRetrying, retry_if_exception, retry_if_result, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.app.testing.models import ThreadView
from agentplane.proto import event_log_pb2, event_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.tracing import tracer
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# A turn on the cheap model with a few tool calls. A harness that stops making progress should fail
# its scenario rather than hold the suite until Bazel's clock runs out.
TURN_SECONDS = 300.0
# The runner's writable volume; the image's workspace is not writable by the runner's user.
WORKING_DIRECTORY = "/state/work"
# How long a Running sandbox may still be short of a connected runner. Generous because the gap is
# a container start, not a fixed cost, and a scenario that waits a few seconds here is cheaper than
# one that fails a whole run.
RUNNER_ANSWERS_SECONDS = 120.0


def _runner_not_answering(error: BaseException) -> bool:
    """The app's `503`, and only that: it means the runner has not answered yet. Its `409`s -- an
    unreachable sandbox, a refused Open -- are verdicts, not delays, and must fail immediately."""
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def runner_startup_retries() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_delay(RUNNER_ANSWERS_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception(_runner_not_answering),
        reraise=True,
    )


Reported = TypeVar("Reported", bound=BaseModel)

# The last brace-delimited object in the answer, fenced or not: a model that explains itself before
# or after the JSON is still answering the question.
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Turn:
    """What one turn produced: the harness's own tool outputs and its assistant text."""

    # `time.monotonic()` when the app accepted the turn's input: the Dispatcher holds it, and no
    # native work need have started.
    accepted: float
    tool_outputs: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    # Every line the harness wrote during the turn, as the runner recorded it.
    native: list[str] = field(default_factory=list)
    status: event_pb2.TurnStatus | None = None
    input_confirmed: bool = False
    resumed: bool = False

    @property
    def answer(self) -> str:
        """The turn's final assistant message: what the agent reports back."""
        if not self.text:
            raise AssertionError(f"the turn produced no assistant message:\n{self.transcript}")
        return self.text[-1]

    def report(self, shape: type[Reported]) -> Reported:
        """The JSON object the agent was asked to end with, as `shape`.

        A scenario states a goal and asks for a structured report, rather than dictating a command
        and grepping prose: the agent picks how, and the answer stays machine-checkable.
        """
        found = _OBJECT.search(self.answer)
        if found is None:
            raise AssertionError(f"the agent reported no JSON object:\n{self.transcript}")
        try:
            return shape.model_validate_json(found.group())
        except (ValueError, TypeError) as error:
            raise AssertionError(f"the agent's report is not a {shape.__name__}: {error}\n{self.answer}") from error

    @property
    def transcript(self) -> str:
        """Everything the turn produced, so a failure says what the agent actually did."""
        return "\n".join([*(f"[tool] {output}" for output in self.tool_outputs), *(f"[text] {t}" for t in self.text)])


class Agent:
    """One Thread. `run` returns only a successfully completed turn."""

    def __init__(
        self,
        client: Client,
        dispatcher: SessionDispatcherClient,
        *,
        thread_id: UUID,
        cursor: int,
        sandbox: str | None = None,
        sandbox_uid: str | None = None,
    ) -> None:
        if (sandbox is None) != (sandbox_uid is None):
            raise ValueError("Sandbox name and UID must be supplied together")
        self._client = client
        self._dispatcher = dispatcher
        self._thread_id = thread_id
        self._cursor = cursor
        self._target = (sandbox, sandbox_uid) if sandbox and sandbox_uid else None

    @property
    def thread_id(self) -> UUID:
        return self._thread_id

    @classmethod
    async def open(
        cls,
        client: Client,
        dispatcher: SessionDispatcherClient,
        *,
        sandbox: str,
        sandbox_uid: UUID,
        harness: runner_pb2.Harness,
        model: str,
        instructions: str = "",
        cwd: str = WORKING_DIRECTORY,
    ) -> Agent:
        """Open a session, waiting out a runner that is up but not yet connected.

        A Running Sandbox's runner may not answer yet. Retry that `503` with the same
        Session ID; other HTTP errors fail immediately.

        `instructions` are the session's standing orders, in front of the model on every turn; empty
        is the proto's own default and opens the session the runner would open without the field.
        """
        with tracer.start_as_current_span(
            "system_test.session.open",
            attributes={"sandbox.name": sandbox, "harness": runner_pb2.Harness.Name(harness)},
        ):
            spec = runner_pb2.SessionSpec(
                harness=harness, cwd=cwd, model=model, reasoning_effort="low", instructions=instructions
            )
            # Retry the same desired session even if an earlier Open reached the runner.
            session_id = uuid4()
            async for attempt in runner_startup_retries():
                with attempt:
                    await client.create_session(
                        sandbox, session_id, sandbox_uid=sandbox_uid, overrides=MessageToDict(spec)
                    )
                    thread = await client.wait_for_thread_projection(session_id, timeout_s=RUNNER_ANSWERS_SECONDS)
                    return cls(
                        client, dispatcher, thread_id=thread.id, cursor=0, sandbox=sandbox, sandbox_uid=str(sandbox_uid)
                    )
            raise AssertionError("unreachable: reraise=True either returns an agent or raises")

    async def run(self, prompt: str) -> Turn:
        """Send `prompt` and collect the turn it starts. Nothing else drives this session, so reading
        the cursor before submitting cannot miss an event."""
        after = self._cursor
        command_id = f"input-{uuid4().hex[:8]}"
        await self.submit_input(command_id, prompt)
        return await self._collect(after=after, awaiting_command_id=command_id)

    async def submit_input(self, command_id: str, text: str) -> session_dispatcher_pb2.Dispatch:
        """Send one command directly to Dispatcher with this Session's pinned target."""
        if self._target is None:
            self._target = await self._dispatcher.session_target(str(self._thread_id))
        sandbox, sandbox_uid = self._target
        return await self._dispatcher.submit_input(str(self._thread_id), command_id, sandbox, sandbox_uid, text)

    async def run_shell(self, model: ScriptedModel[Any], command: str) -> Turn:
        """A turn in which the scripted `model` runs `command` in the Sandbox's shell, then answers."""
        async with asyncio.TaskGroup() as turns:
            turn = turns.create_task(self.run(f"Run: {command}"))
            await model.reply(await model.request(), ShellCall(call_id=f"call-{uuid4().hex[:8]}", command=command))
            await model.reply(await model.request(), Text("Done."))
        return turn.result()

    async def next_turn(self, *, awaiting_command_id: str | None = None) -> Turn:
        """Collect the next turn after the last one collected, whatever started it: an inbox notice
        the platform delivered, for one. Pass a command ID when the caller is about to submit that
        command; this keeps a retained terminal snapshot from ending the stream before admission.
        `accepted` is when collecting began."""
        return await self._collect(after=self._cursor, awaiting_command_id=awaiting_command_id)

    async def _collect(self, *, after: int, awaiting_command_id: str | None) -> Turn:
        with tracer.start_as_current_span("system_test.turn.wait", attributes={"thread.id": str(self._thread_id)}):
            turn = Turn(accepted=time.monotonic())
            command_admitted = awaiting_command_id is None
            async with (
                asyncio.timeout(TURN_SECONDS),
                aclosing(
                    self._client.events(
                        self._thread_id, after=after, read_seconds=TURN_SECONDS, awaiting_command_id=awaiting_command_id
                    )
                ) as entries,
            ):
                async for entry in entries:
                    self._cursor = entry.cursor
                    event = entry.event
                    match event.WhichOneof("observation"):
                        case "command_admitted" if event.command_admitted.command.command_id == awaiting_command_id:
                            command_admitted = True
                        case "command_failed" if (
                            awaiting_command_id is None or event.command_failed.command_id == awaiting_command_id
                        ):
                            failed = event.command_failed
                            raise AssertionError(f"command {failed.command_id!r} failed: {failed.reason!r}")
                        case "harness_launch_failed" if command_admitted:
                            raise AssertionError(f"harness launch failed: {event.harness_launch_failed.reason!r}")
                        case "harness_started":
                            turn.resumed = event.harness_started.resumed
                        case "harness_user_message_confirmed":
                            turn.input_confirmed = True
                        case "item_completed":
                            _completed(event.item_completed, turn)
                        case "native" if event.native.direction == event_pb2.DIRECTION_FROM_HARNESS:
                            turn.native.append(event.native.line)
                        case "turn_completed":
                            completed = event.turn_completed
                            turn.status = completed.status
                            if turn.status != event_pb2.TURN_STATUS_COMPLETED:
                                raise AssertionError(
                                    f"turn {completed.turn_id!r} ended with {event_pb2.TurnStatus.Name(turn.status)}: "
                                    f"{completed.error=}"
                                )
                            return turn
                        case _:
                            continue
            raise AssertionError(f"the session's stream ended before the turn completed:\n{turn.transcript}")


def _completed(item: event_pb2.ItemCompleted, turn: Turn) -> None:
    match item.WhichOneof("outcome"):
        case "tool":
            turn.tool_outputs.append(item.tool.output)
        case "text":
            turn.text.append(item.text)


async def scripted_turn_with_admission_and_history(
    client: Client,
    agent: Agent,
    *,
    command_id: str,
    prompt: str,
    model: ScriptedModel[Any],
    history_turns: int = 1,
    shell_command: str | None = None,
    shell_call_id: str | None = None,
    response: str = "Done.",
) -> tuple[Turn, list[event_log_pb2.EventEntry], ModelRequest[Any]]:
    async with asyncio.TaskGroup() as turns:
        collecting = turns.create_task(agent.next_turn(awaiting_command_id=command_id))
        admission = await agent.submit_input(command_id, prompt)
        assert admission.command_id == command_id
        assert admission.operation == session_dispatcher_pb2.OPERATION_SUBMIT_INPUT

        request = await model.request()
        assert prompt in request.user_texts
        if shell_command is None:
            await model.reply(request, Text(response))
        else:
            call_id = shell_call_id or f"call-{uuid4().hex[:8]}"
            await model.reply(request, ShellCall(call_id=call_id, command=shell_command))
            tool_result = await model.request()
            try:
                assert any(output.call_id == call_id for output in tool_result.tool_outputs), tool_result.tool_outputs
            finally:
                # Complete the scripted turn even when the tool result is unexpected.
                await model.reply(tool_result, Text(response))
    turn = collecting.result()
    assert turn.status == event_pb2.TURN_STATUS_COMPLETED
    entries = await _history(client, agent.thread_id, completed_turns=history_turns)
    assert any(
        entry.event.HasField("command_admitted") and entry.event.command_admitted.command.command_id == command_id
        for entry in entries
    ), f"History did not retain command admission {command_id}"
    # FollowSession exposes committed archive entries before History's asynchronous fold applies
    # them. The Thread summary is authoritative only once it covers this completed turn.
    view: ThreadView = await AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(0.05),
        retry=retry_if_result(lambda row: row.last_cursor < entries[-1].cursor),
    )(client.thread, agent.thread_id)
    assert view.last_turn_status == "TURN_STATUS_COMPLETED"
    return turn, entries, request


async def _history(client: Client, thread_id: UUID, *, completed_turns: int) -> list[event_log_pb2.EventEntry]:
    entries: list[event_log_pb2.EventEntry] = []
    observed_turns = 0
    async with asyncio.timeout(TURN_SECONDS):
        async for entry in client.events(thread_id, after=0, read_seconds=TURN_SECONDS):
            entries.append(entry)
            if entry.event.HasField("turn_completed"):
                observed_turns += 1
                if observed_turns == completed_turns:
                    break
    assert observed_turns == completed_turns, f"History ended after {observed_turns} completed turns"
    return entries
