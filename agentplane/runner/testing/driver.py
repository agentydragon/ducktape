"""Drive a runner as the Runner Gateway and its clients do: calls to its `Runner` service over the
runner's control stream, and each session's Events as its ingress committed them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import grpc
import pytest

from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_pb2
from agentplane.runner.config import RunnerConfig
from agentplane.runner.runner import Runner
from agentplane.runner.testing.gateway import Gateway, dialing

# The `Runner` stub types its calls; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/proto:runner_control_pb2_grpc
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf


class StreamClosedError(Exception):
    """The session has nothing more to run, and every Event it journaled is read."""


class SessionSealedError(StreamClosedError):
    """The session's journal is sealed, and read to its seal at `cursor`."""

    def __init__(self, cursor: int) -> None:
        super().__init__(f"{cursor=}")
        self.cursor = cursor


async def refusal(operation: Awaitable[object]) -> grpc.StatusCode:
    """The status the runner refused the call `operation` makes with."""
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await operation
    return refused.value.code()


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
        runner = await self._gateway.control.runner()
        await runner.DeliverCommand(
            runner_control_pb2.DeliverCommandRequest(session_id=self.attached.session_id, command=command)
        )

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

    async def attach(
        self,
        session_id: str,
        *,
        spec: runner_pb2.SessionSpec | None = None,
        setup_script: str | None = None,
        after_cursor: int = 0,
    ) -> SessionFollow:
        """Follow a session after `after_cursor`. With `spec`, open it first as a client reopening a
        session does: create it, or confirm it and start its stopped harness unless its setup failed."""
        if spec is None:
            summary = next((found for found in await self.list_sessions() if found.session_id == session_id), None)
            if summary is None:
                raise grpc.aio.AioRpcError(
                    grpc.StatusCode.NOT_FOUND, grpc.aio.Metadata(), grpc.aio.Metadata(), f"no session {session_id}"
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
        runner = await self.gateway.control.runner()
        opening = runner_control_pb2.OpenSessionRequest(session_id=session_id, spec=spec)
        if setup_script is not None:
            opening.setup_script = setup_script
        attached = (await runner.OpenSession(opening)).attached
        return SessionFollow(self.gateway, attached, after_cursor)

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        runner = await self.gateway.control.runner()
        return list((await runner.ListSessions(runner_pb2.ListSessionsRequest())).sessions)

    async def seal(self) -> runner_pb2.QuiesceResponse:
        """Seal the Sandbox and return once the ingress holds every session's seal, as teardown waits."""
        sealed = await (await self.gateway.control.runner()).Quiesce(runner_pb2.QuiesceRequest())
        for session in sealed.sessions:
            await self.gateway.ingress.wait_sealed(session.session_id)
        return sealed


@asynccontextmanager
async def running(config: RunnerConfig, gateway: Gateway) -> AsyncIterator[Runner]:
    """A started runner on `config`, dialing `gateway`."""
    async with Runner(config) as runner:
        await runner.startup()
        async with dialing(runner, gateway):
            yield runner
