"""Drive a runner as the Runner Gateway and its clients do: lifecycle requests and commands over the
runner's control stream, and each session's Events as its ingress committed them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.service import Runner
from agentplane.runner.testing.gateway import Gateway, dialing
from agentplane.runner_gateway.command_delivery import CommandRefusedError
from agentplane.runner_gateway.lifecycle_control import ControlRefusedError

# gazelle:include_dep @pypi//protobuf


class StreamClosedError(Exception):
    """The session has nothing more to run, and every Event it journaled is read."""


class SessionSealedError(StreamClosedError):
    """The session's journal is sealed, and read to its seal at `cursor`."""

    def __init__(self, cursor: int) -> None:
        super().__init__(f"{cursor=}")
        self.cursor = cursor


async def refusal(operation: Awaitable[object]) -> runner_control_pb2.ControlRefused.Reason:
    """Why the runner refused the lifecycle request `operation` makes."""
    with pytest.raises(ControlRefusedError) as refused:
        await operation
    return refused.value.refusal.reason


class SessionFollow:
    """One session's Events after a cursor, from the snapshot `attached` on. Reading ends once the
    session has nothing more to run: its harness exited, its setup or launch failed, or it was stopped
    when the follow opened. A later run of the harness is a new follow's."""

    def __init__(self, gateway: Gateway, attached: runner_pb2.Attached, after_cursor: int) -> None:
        self._gateway = gateway
        self.attached = attached
        self.seen: list[event_log_pb2.EventEntry] = []
        self._cursor = after_cursor
        self._ended = (
            attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
            and attached.setup_state != runner_pb2.SETUP_STATE_RUNNING
        )

    @property
    def cursor(self) -> int:
        """The last source-local cursor read; a later follow continues after it."""
        return self._cursor

    async def command(self, command: command_pb2.Command) -> None:
        """Deliver `command` and return once the runner admitted it."""
        receipt = await self._gateway.control.deliver(
            runner_control_pb2.DeliverCommand(session_id=self.attached.session_id, command=command)
        )
        if receipt.HasField("refused"):
            raise CommandRefusedError(receipt.refused)

    async def stop_runner_session(self, command_id: str) -> None:
        await self.command(
            command_pb2.Command(command_id=command_id, stop_runner_session=command_pb2.StopRunnerSession())
        )

    async def next_entry(self) -> event_log_pb2.EventEntry:
        ingress = self._gateway.ingress
        session_id = self.attached.session_id
        while not (page := ingress.since(session_id, self._cursor)):
            if (sealed := ingress.sealed.get(session_id)) is not None and self._cursor >= sealed:
                raise SessionSealedError(sealed)
            # The ingress may still lag the snapshot; what the snapshot covers is always read.
            if self._ended and self._cursor >= self.attached.last_cursor:
                raise StreamClosedError
            await ingress.wait_beyond(session_id, self._cursor)
        entry = page[0]
        event = entry.event
        if self._ended and entry.cursor > self.attached.last_cursor and event.HasField("harness_started"):
            raise StreamClosedError
        self._cursor = entry.cursor
        self.seen.append(entry)
        if entry.cursor > self.attached.last_cursor and event.HasField("harness_exited"):
            self._ended = True
        if event.HasField("setup_finished"):
            self._ended = event.setup_finished.exit_code != 0
        if event.HasField("setup_interrupted") or event.HasField("harness_launch_failed"):
            self._ended = True
        return entry

    async def until(
        self, accept: Callable[[event_log_pb2.EventEntry], bool], *, timeout_s: float = 60
    ) -> event_log_pb2.EventEntry:
        """Read entries until one satisfies `accept`, and return it."""
        async with asyncio.timeout(timeout_s):
            while not accept(entry := await self.next_entry()):
                pass
            return entry

    async def drain_until_end(self) -> None:
        try:
            while True:
                await self.next_entry()
        except StreamClosedError:
            return


class RunnerDriver:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    async def _request(self, request: runner_control_pb2.ControlRequest) -> runner_control_pb2.ControlResult:
        result = await self.gateway.control.request(request)
        if result.HasField("refused"):
            raise ControlRefusedError(result.refused)
        return result

    async def attach(
        self,
        session_id: str,
        *,
        spec: runner_pb2.SessionSpec | None = None,
        setup_script: str | None = None,
        after_cursor: int = 0,
    ) -> SessionFollow:
        """Follow a session after `after_cursor`. With `spec`, open it first as a client reopening a
        session does: create it, or confirm it and resume its stopped harness unless its setup failed."""
        if spec is None:
            summary = next((found for found in await self.list_sessions() if found.session_id == session_id), None)
            if summary is None:
                raise ControlRefusedError(
                    runner_control_pb2.ControlRefused(
                        reason=runner_control_pb2.ControlRefused.REASON_SESSION_UNKNOWN, detail=session_id
                    )
                )
            attached = runner_pb2.Attached(
                session_id=summary.session_id,
                spec=summary.spec,
                last_cursor=summary.last_cursor,
                harness_state=summary.harness_state,
                active_turn_id=summary.active_turn_id,
                setup_state=summary.setup_state,
                launch_outcome=summary.launch_outcome,
            )
            return SessionFollow(self.gateway, attached, after_cursor)
        opening = runner_control_pb2.OpenSession(session_id=session_id, spec=spec)
        if setup_script is not None:
            opening.setup_script = setup_script
        attached = (await self._request(runner_control_pb2.ControlRequest(open=opening))).open
        if attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED and attached.setup_state in (
            runner_pb2.SETUP_STATE_NOT_REQUIRED,
            runner_pb2.SETUP_STATE_SUCCEEDED,
        ):
            resuming = runner_control_pb2.ResumeSession(session_id=session_id)
            attached = (await self._request(runner_control_pb2.ControlRequest(resume=resuming))).resume
        return SessionFollow(self.gateway, attached, after_cursor)

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        listing = runner_control_pb2.ControlRequest(list_sessions=runner_pb2.ListSessionsRequest())
        return list((await self._request(listing)).list_sessions.sessions)

    async def seal(self) -> runner_pb2.SealResponse:
        """Seal the Sandbox and return once the ingress holds every session's seal, as teardown waits."""
        sealed = (await self._request(runner_control_pb2.ControlRequest(seal=runner_pb2.SealRequest()))).seal
        for session in sealed.sessions:
            await self.gateway.ingress.wait_sealed(session.session_id)
        return sealed

    async def initialize(self, script: str) -> runner_pb2.InitializationEvent:
        """The bootstrap's terminal event, `result` or `interrupted`, once the attempt has ended."""
        initializing = runner_control_pb2.ControlRequest(initialize=runner_control_pb2.Initialize(script=script))
        return (await self._request(initializing)).initialize


@asynccontextmanager
async def running(config: RunnerConfig, gateway: Gateway) -> AsyncIterator[Runner]:
    """A started runner on `config`, dialing `gateway`."""
    async with Runner(config) as runner:
        await runner.startup()
        async with dialing(runner, gateway):
            yield runner
