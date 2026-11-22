"""Drive an environment-hosted Session through its backend authorities, without App."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import grpc

from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.session_events import SessionEventPages
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient, accepted_session
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.driver.turn import TURN_SECONDS, Turn, TurnCollector
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text


class Session:
    """One exact backend Session; mutations are submitted once, reads can reconnect."""

    def __init__(
        self,
        dispatcher: SessionDispatcherClient,
        history: HistoryServiceClient,
        *,
        session_id: UUID,
        environment: session_pb2.ExactEnvironment,
        cursor: int = 0,
    ) -> None:
        if not environment.name or not environment.uid:
            raise ValueError("Session requires an exact Environment name and UID")
        self._dispatcher = dispatcher
        self._history = history
        self.session_id = session_id
        # Keep the immutable placement, even if the caller later mutates its protobuf object.
        self._environment = session_pb2.ExactEnvironment()
        self._environment.CopyFrom(environment)
        self._cursor = cursor
        self._history_attached = False

    @property
    def cursor(self) -> int:
        """Last consumed History event, including the terminal event of a completed turn."""
        return self._cursor

    @classmethod
    async def create(
        cls,
        dispatcher: SessionDispatcherClient,
        history: HistoryServiceClient,
        *,
        spec: session_pb2.SessionSpec,
        session_id: UUID,
        setup_script: str | None = None,
    ) -> Session:
        if spec.placement.WhichOneof("kind") != "environment_hosted":
            raise ValueError("platform Session requires environment-hosted placement")
        environment = spec.placement.environment_hosted.environment
        if not environment.name or not environment.uid:
            raise ValueError("Session requires an exact Environment name and UID")
        request = session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec)
        if setup_script is not None:
            request.setup_script = setup_script
        with tracer.start_as_current_span(
            "system_test.session.create",
            attributes={
                "session.id": str(session_id),
                "sandbox.name": environment.name,
                "sandbox.uid": environment.uid,
                "harness": session_pb2.Harness.Name(spec.placement.environment_hosted.harness),
            },
        ):
            # An uncertain create is not permission to replay a mutation. The caller retains the
            # Session ID for diagnostics and reconciliation if submission itself fails.
            await dispatcher.unary(dispatcher.stub.CreateSession, request)
            await accepted_session(dispatcher, session_id)
        return cls(
            dispatcher,
            history,
            session_id=session_id,
            environment=request.spec.placement.environment_hosted.environment,
        )

    async def submit_input(self, command_id: str, text: str) -> session_dispatcher_pb2.Dispatch:
        return await self._dispatcher.submit_input(
            str(self.session_id), command_id, self._environment.name, self._environment.uid, text
        )

    async def run(self, prompt: str) -> Turn:
        command_id = f"input-{uuid4().hex[:8]}"
        await self.submit_input(command_id, prompt)
        return await self.next_turn(awaiting_command_id=command_id)

    async def run_shell(self, model: ScriptedModel[Any], command: str) -> Turn:
        async with asyncio.TaskGroup() as turns:
            turn = turns.create_task(self.run(f"Run: {command}"))
            await model.reply(await model.request(), ShellCall(call_id=f"call-{uuid4().hex[:8]}", command=command))
            await model.reply(await model.request(), Text("Done."))
        return turn.result()

    async def next_turn(self, *, awaiting_command_id: str | None = None) -> Turn:
        collector = TurnCollector(
            after_cursor=self._cursor, awaiting_command_id=awaiting_command_id, accepted=time.monotonic()
        )
        with tracer.start_as_current_span("system_test.turn.wait", attributes={"session.id": str(self.session_id)}):
            try:
                async with asyncio.timeout(TURN_SECONDS):
                    async for pages in self._pages(after_cursor=self._cursor):
                        if (turn := collector.collect(pages.entries)) is not None:
                            return turn
                        collector.cursor = pages.cursor
            finally:
                self._cursor = collector.cursor
        raise AssertionError("History pagination ended before the turn completed")

    async def setup_output(self) -> tuple[str, str]:
        """Require successful setup and return its retained stdout/stderr, without App projection."""
        with tracer.start_as_current_span(
            "system_test.session_setup.output_wait", attributes={"session.id": str(self.session_id)}
        ):
            async with asyncio.timeout(TURN_SECONDS):
                async for pages in self._pages(after_cursor=self._cursor):
                    terminal = next(
                        (
                            entry
                            for entry in pages.entries
                            if entry.event.HasField("setup_finished") or entry.event.HasField("setup_interrupted")
                        ),
                        None,
                    )
                    if terminal is None:
                        self._cursor = pages.cursor
                        continue
                    self._cursor = terminal.cursor
                    stdout, stderr = bytearray(), bytearray()
                    for entry in pages.entries:
                        if entry.cursor > terminal.cursor:
                            break
                        if entry.event.HasField("setup_output"):
                            stdout.extend(entry.event.setup_output.stdout)
                            stderr.extend(entry.event.setup_output.stderr)
                    output, errors = stdout.decode(errors="replace"), stderr.decode(errors="replace")
                    if terminal.event.HasField("setup_interrupted"):
                        raise AssertionError(f"setup for Session {self.session_id} was interrupted: {errors}")
                    assert terminal.event.setup_finished.exit_code == 0, (
                        f"setup for Session {self.session_id} exited {terminal.event.setup_finished.exit_code}: "
                        f"{errors}\n{output}"
                    )
                    return output, errors
        raise AssertionError(f"setup for Session {self.session_id} emitted no finished event")

    async def _pages(self, *, after_cursor: int) -> AsyncIterator[SessionEventPages]:
        pages = SessionEventPages(after_cursor=after_cursor)
        while True:
            try:
                page = await self._history.read_session_events(
                    str(self.session_id), after_cursor=pages.cursor, limit=pages.limit
                )
            except ServiceError as error:
                # Dispatcher acceptance can precede History attachment. Once a healthy read
                # succeeds, disappearance is a failure, not another attachment.
                if error.code != grpc.StatusCode.UNAVAILABLE and not (
                    error.code == grpc.StatusCode.NOT_FOUND and not self._history_attached
                ):
                    raise
                await asyncio.sleep(0.2)
                continue
            except TimeoutError:
                # Read-only reconnects stay inside the operation's single deadline.
                await asyncio.sleep(0.2)
                continue
            pages.apply_page(page)
            self._history_attached = True
            yield pages
            if not pages.more_available:
                await asyncio.sleep(0.2)


# gazelle:include_dep @pypi//protobuf


async def run_session_setup_script(
    dispatcher: SessionDispatcherClient,
    history: HistoryServiceClient,
    *,
    sandbox_name: str,
    sandbox_uid: str,
    session_id: UUID,
    harness: session_pb2.Harness,
    model: str,
    cwd: str,
    setup_script: str,
) -> tuple[str, str]:
    """Create a Session on an existing Sandbox and return its successful setup script output."""
    session = await Session.create(
        dispatcher,
        history,
        session_id=session_id,
        setup_script=setup_script,
        spec=session_pb2.SessionSpec(
            placement=session_pb2.SessionPlacement(
                environment_hosted=session_pb2.EnvironmentHostedPlacement(
                    environment=session_pb2.ExactEnvironment(name=sandbox_name, uid=sandbox_uid), harness=harness
                )
            ),
            cwd=cwd,
            model=model,
            reasoning_effort="low",
        ),
    )
    return await session.setup_output()
