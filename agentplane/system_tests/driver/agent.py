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
import time
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4

import httpx
from google.protobuf.json_format import MessageToDict
from tenacity import AsyncRetrying, retry_if_exception, retry_if_result, stop_after_delay, wait_fixed

from agentplane.app.testing.client import REQUEST_SECONDS, Client
from agentplane.app.testing.history_reads import SessionEventReader
from agentplane.app.testing.models import ThreadView
from agentplane.history_service.api.history import history_service_pb2
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.scripted_turn import drive_scripted_turn
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.driver.turn import TURN_SECONDS, Turn, TurnCollector
from agentplane.testing.scripted_model import ModelRequest, ScriptedModel, ShellCall, Text

# The runner's writable volume; the image's workspace is not writable by the runner's user.
WORKING_DIRECTORY = "/state/work"
# How long a Running sandbox may still be short of a connected runner. Generous because the gap is
# a container start, not a fixed cost, and a scenario that waits a few seconds here is cheaper than
# one that fails a whole run.
RUNNER_ANSWERS_SECONDS = 120.0


def _runner_not_answering(error: BaseException) -> bool:
    """The app's `503`, and only that: it means the runner has not answered yet. Its `409`s -- an
    unreachable sandbox, a refused Create -- are verdicts, not delays, and must fail immediately."""
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def environment_runtime_startup_retries() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_delay(RUNNER_ANSWERS_SECONDS),
        wait=wait_fixed(2),
        retry=retry_if_exception(_runner_not_answering),
        reraise=True,
    )


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
    async def create(
        cls,
        client: Client,
        dispatcher: SessionDispatcherClient,
        *,
        sandbox: str,
        sandbox_uid: str,
        harness: session_pb2.Harness,
        model: str,
        instructions: str = "",
        cwd: str = WORKING_DIRECTORY,
    ) -> Agent:
        """Create a Session, waiting out a runner that is up but not yet connected.

        A Running Sandbox's runner may not answer yet. Retry that `503` with the same
        Session ID; other HTTP errors fail immediately.

        `instructions` are the session's standing orders, in front of the model on every turn; empty
        is the proto's own default and creates the session the runner would create without the field.
        """
        with tracer.start_as_current_span(
            "system_test.session.create",
            attributes={"sandbox.name": sandbox, "harness": session_pb2.Harness.Name(harness)},
        ):
            spec = session_pb2.SessionSpec(
                placement=session_pb2.SessionPlacement(
                    environment_hosted=session_pb2.EnvironmentHostedPlacement(
                        environment=session_pb2.ExactEnvironment(name=sandbox, uid=sandbox_uid), harness=harness
                    )
                ),
                cwd=cwd,
                model=model,
                reasoning_effort="low",
                instructions=instructions,
            )
            # Retry the same desired Session even if an earlier create reached the runner.
            session_id = uuid4()
            async for attempt in environment_runtime_startup_retries():
                with attempt:
                    await client.create_session_and_wait(
                        sandbox,
                        session_id,
                        dispatcher=dispatcher,
                        sandbox_uid=sandbox_uid,
                        overrides=MessageToDict(spec),
                    )
                    thread = await client.wait_for_thread_projection(session_id, timeout_s=RUNNER_ANSWERS_SECONDS)
                    return cls(
                        client, dispatcher, thread_id=thread.id, cursor=0, sandbox=sandbox, sandbox_uid=sandbox_uid
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
            collector = TurnCollector(
                after_cursor=after, awaiting_command_id=awaiting_command_id, accepted=time.monotonic()
            )
            reader = SessionEventReader(self._client, self._thread_id, after_cursor=after)
            try:
                async with asyncio.timeout(TURN_SECONDS):
                    while True:
                        await reader.read_page()
                        if (turn := collector.collect(reader.entries)) is not None:
                            return turn
                        collector.cursor = reader.cursor
                        await reader.wait_for_next_page()
            finally:
                self._cursor = collector.cursor


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
    turn, request = await drive_scripted_turn(
        agent,
        command_id=command_id,
        prompt=prompt,
        model=model,
        shell_command=shell_command,
        shell_call_id=shell_call_id,
        response=response,
    )
    entries = await _history(client, agent.thread_id, completed_turns=history_turns)
    assert any(
        entry.event.HasField("command_admitted") and entry.event.command_admitted.command.command_id == command_id
        for entry in entries
    ), f"History did not retain command admission {command_id}"
    # FollowSession exposes committed archive entries before History's asynchronous fold applies
    # them. The Thread summary is authoritative only once it covers this completed turn.
    summary: history_service_pb2.SessionSummary = await AsyncRetrying(
        stop=stop_after_delay(REQUEST_SECONDS),
        wait=wait_fixed(0.05),
        retry=retry_if_result(
            lambda row: row.committed_cursor < entries[-1].cursor or row.folded_through < entries[-1].cursor
        ),
    )(client.session_summary, agent.thread_id)
    assert summary.committed_cursor >= entries[-1].cursor
    assert summary.folded_through >= entries[-1].cursor
    view: ThreadView = await client.thread(agent.thread_id)
    assert view.last_turn_status == "TURN_STATUS_COMPLETED"
    return turn, entries, request


async def _history(client: Client, thread_id: UUID, *, completed_turns: int) -> list[event_log_pb2.EventEntry]:
    reader = SessionEventReader(client, thread_id, after_cursor=0)
    observed_turns = 0
    terminal_cursor: int | None = None
    async with asyncio.timeout(TURN_SECONDS):
        while terminal_cursor is None:
            entries = await reader.read_page()
            for entry in entries:
                if entry.event.HasField("turn_completed"):
                    observed_turns += 1
                    if observed_turns == completed_turns:
                        terminal_cursor = entry.cursor
                        break
            if terminal_cursor is None:
                await reader.wait_for_next_page()
    assert observed_turns == completed_turns, f"History ended after {observed_turns} completed turns"
    return [entry for entry in reader.entries if entry.cursor <= terminal_cursor]
