"""Drive a runner as the Runner Gateway and its clients do: calls to its `Runner` service over the
runner's control stream, and each session's Events as its ingress committed them."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import grpc
import pytest

from agentplane.proto import event_log_pb2, runner_pb2
from agentplane.runner import runner_service_pb2
from agentplane.runner.testing.config import RunnerTestConfig
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway import runner_control_pb2

# The `Runner` stub types its calls; mypy reads stubs only from direct deps.
# gazelle:include_dep //agentplane/runner:runner_service_pb2_grpc
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
    session has nothing more to run: its harness exited or its setup or launch failed. A command
    submitted through this follow may wake a dormant session and continue the same event stream."""

    def __init__(self, gateway: Gateway, attached: runner_pb2.Attached, after_cursor: int) -> None:
        self._gateway = gateway
        self.attached = attached
        self.seen: list[event_log_pb2.EventEntry] = []
        self._cursor = after_cursor
        self._ended = (
            attached.harness_state == runner_pb2.HARNESS_STATE_STOPPED
            and attached.setup_state != runner_pb2.SETUP_STATE_RUNNING
            and attached.launch_outcome != runner_pb2.LAUNCH_OUTCOME_PENDING
        )

    @property
    def cursor(self) -> int:
        """The last source-local cursor read; a later follow continues after it."""
        return self._cursor

    async def submit_input(self, command_id: str, text: str) -> None:
        """Submit text and return once the runner admitted it."""
        runner = await self._gateway.control.runner()
        await runner.SubmitInput(
            runner_service_pb2.SubmitInputRequest(session_id=self.attached.session_id, command_id=command_id, text=text)
        )
        self._ended = False

    async def interrupt_turn(self, command_id: str, turn_id: str) -> None:
        runner = await self._gateway.control.runner()
        await runner.InterruptTurn(
            runner_service_pb2.InterruptTurnRequest(
                session_id=self.attached.session_id, command_id=command_id, turn_id=turn_id
            )
        )

    async def change_model(self, command_id: str, model: str) -> None:
        runner = await self._gateway.control.runner()
        await runner.ChangeModel(
            runner_service_pb2.ChangeModelRequest(
                session_id=self.attached.session_id, command_id=command_id, model=model
            )
        )

    async def change_reasoning_effort(self, command_id: str, effort: str) -> None:
        runner = await self._gateway.control.runner()
        await runner.ChangeReasoningEffort(
            runner_service_pb2.ChangeReasoningEffortRequest(
                session_id=self.attached.session_id, command_id=command_id, effort=effort
            )
        )

    async def next_entry(self) -> event_log_pb2.EventEntry:
        ingress = self._gateway.ingress
        session_id = self.attached.session_id
        while not (page := ingress.since(session_id, self._cursor)):
            if (sealed := ingress.sealed.get(session_id)) is not None and self._cursor >= sealed:
                raise SessionSealedError(sealed)
            # Process exit can precede activation's command settlement. Read through the
            # runner's current published cursor once that work finishes, even if ingress lags.
            if self._ended and self._cursor >= self.attached.last_cursor:
                runner = await self._gateway.control.runner()
                status = await runner.GetStatus(runner_pb2.GetStatusRequest())
                session = next(row for row in status.sessions if row.session_id == session_id)
                if session.busy:
                    # Activation can finish after publishing its final Event, so completion
                    # need not produce another ingress wakeup.
                    await asyncio.sleep(0.01)
                    continue
                summaries = await runner.ListSessions(runner_pb2.ListSessionsRequest())
                summary = next(row for row in summaries.sessions if row.session_id == session_id)
                if summary.harness_state == runner_pb2.HARNESS_STATE_STOPPED and self._cursor >= summary.last_cursor:
                    raise StreamClosedError
            await ingress.wait_beyond(session_id, self._cursor)
        entry = page[0]
        event = entry.event
        self._cursor = entry.cursor
        self.seen.append(entry)
        if entry.cursor > self.attached.last_cursor and event.HasField("harness_started"):
            self._ended = False
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
        """Follow after `after_cursor`, durably reserving or confirming the session when given a spec."""
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
        opening = runner_service_pb2.OpenSessionRequest(session_id=session_id, spec=spec)
        if setup_script is not None:
            opening.setup_script = setup_script
        attached = (await runner.OpenSession(opening)).attached
        return SessionFollow(self.gateway, attached, after_cursor)

    async def list_sessions(self) -> list[runner_pb2.SessionSummary]:
        runner = await self.gateway.control.runner()
        return list((await runner.ListSessions(runner_pb2.ListSessionsRequest())).sessions)

    async def quiesce(self) -> runner_pb2.QuiesceResponse:
        """Request quiesce, then wait until the runner has sealed and the ingress has every Session."""
        runner = await self.gateway.control.runner()
        response = await runner.Quiesce(runner_pb2.QuiesceRequest())
        sessions = await runner.ListSessions(runner_pb2.ListSessionsRequest())
        for session in sessions.sessions:
            await self.gateway.ingress.wait_sealed(session.session_id)
        await self.gateway.control.wait_for_facts(
            runner_control_pb2.RunnerFacts(quiesce_stage=runner_control_pb2.RunnerFacts.QUIESCE_STAGE_QUIESCED)
        )
        return response


@asynccontextmanager
async def running(config: RunnerTestConfig, gateway: Gateway) -> AsyncIterator[RunningRunner]:
    """Start the Go executable on this state directory and test Gateway."""
    async with RunningRunner(config, gateway) as runner:
        yield runner
