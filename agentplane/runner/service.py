"""The gRPC surface: Attach streams over the sessions one runner process owns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, aclosing, suppress
from pathlib import Path, PurePosixPath
from typing import Any

import grpc

from agentplane.proto import event_log_pb2, runner_pb2, runner_pb2_grpc
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.claude import ClaudeAdapter
from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.initialization import InitializationLog
from agentplane.runner.journal import Journal, RecoveryState
from agentplane.runner.model_config import ModelConfigLookupError
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord, SessionStore, StateOwner, validate_session_id

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio

logger = logging.getLogger(__name__)
_MAX_BOOTSTRAP_BYTES = 65_536
_FOLLOW_PAGE = 128


class OpenError(Exception):
    """A session operation the runner refuses; an Attach stream ends with this message."""


class SessionUnknownError(OpenError):
    pass


class SpecConflictError(OpenError):
    """The session exists with a different spec or setup script."""


class SandboxSealedError(OpenError):
    pass


class SetupFailedError(OpenError):
    """The session's setup failed or was interrupted, so it never resumes."""


class InvalidRequestError(OpenError):
    """A malformed session ID, spec or setup script."""


class InitializationConflictError(Exception):
    """The sandbox already selected a different bootstrap initialization."""


def make_adapter(session: Session) -> HarnessAdapter:
    harness = runner_pb2.Harness.Value(session.record.harness)
    if harness == runner_pb2.HARNESS_CLAUDE:
        if session.config.claude is None:
            raise RuntimeError("this runner is not configured for Claude sessions")
        return ClaudeAdapter(session, session.config.claude)
    if harness == runner_pb2.HARNESS_CODEX:
        if session.config.codex is None:
            raise RuntimeError("this runner is not configured for Codex sessions")
        return CodexAdapter(session, session.config.codex)
    raise ValueError(f"unsupported {harness=}")


class Runner:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self._state_owner = StateOwner(config.state_dir)
        try:
            if config.process_isolation is not None:
                config.process_isolation.prepare()
            self.store = SessionStore(config.state_dir / "sessions")
        except BaseException:
            self._state_owner.close()
            raise
        self.sessions: dict[str, Session] = {}
        # Called with each Session as this process first loads it, such as when it is created.
        self.session_loaded: list[Callable[[Session], None]] = []
        self._sessions_lock = asyncio.Lock()
        # Held from the existence check through writing the record, so two concurrent creates of one
        # session cannot each write a record with its own event source.
        self._create_lock = asyncio.Lock()
        self._resources = AsyncExitStack()
        self._initialize_lock = asyncio.Lock()
        self._initialization_log: InitializationLog | None = None
        self._initialization_task: asyncio.Task[None] | None = None

    async def initialize(
        self, request: runner_pb2.InitializeRequest
    ) -> tuple[InitializationLog, asyncio.Task[None] | None]:
        """Select one sandbox bootstrap and return its replayable log and current execution."""
        source = request.script.encode()
        if not source or len(source) > _MAX_BOOTSTRAP_BYTES:
            raise ValueError(f"initialize.script must contain 1..{_MAX_BOOTSTRAP_BYTES} UTF-8 bytes")
        async with self._initialize_lock:
            log, selected_now = self._claim_initialization(source)
            if log.ended:
                return log, None
            if self._initialization_task is not None and not self._initialization_task.done():
                return log, self._initialization_task
            if selected_now:
                self._initialization_task = asyncio.create_task(
                    self._execute_initialization(source, 1, log), name="sandbox-initialization"
                )
                return log, self._initialization_task
            # Selected earlier with no recorded end and nothing running it now: the script may have
            # run any part of itself. Never run it again; a fresh initialization needs a new Sandbox.
            log.append_interrupted(max(log.last_attempt, 1))
            return log, None

    def _claim_initialization(self, source: bytes) -> tuple[InitializationLog, bool]:
        """The selection's log, and whether this call made the selection."""
        root = self.config.state_dir / "initialization"
        metadata_path = root / "request.json"
        requested = {"script_sha256": hashlib.sha256(source).hexdigest()}
        if metadata_path.exists():
            selected = json.loads(metadata_path.read_text())
            if selected != requested:
                raise InitializationConflictError(
                    "sandbox already selected a different bootstrap script; refusing another"
                )
            selected_now = False
        else:
            root.mkdir(parents=True, exist_ok=True)
            staged = metadata_path.with_suffix(".json.tmp")
            with staged.open("wb") as output:
                output.write((json.dumps(requested, sort_keys=True) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            staged.replace(metadata_path)
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            selected_now = True
        if self._initialization_log is None:
            self._initialization_log = InitializationLog(root / "events.jsonl")
        return self._initialization_log, selected_now

    async def _execute_initialization(self, source: bytes, attempt: int, log: InitializationLog) -> None:
        process = HarnessProcess(
            ["/bin/sh", "-eu"],
            cwd=self.config.initialization_cwd or self.config.state_dir,
            environment={**os.environ, **self.config.harness_environment},
            state_owner_descriptor=self._state_owner.descriptor,
            process_isolation=self.config.process_isolation,
        )
        await process.start()
        stdout = asyncio.create_task(
            self._record_initialization_output(
                process.output_chunks(), attempt, runner_pb2.INITIALIZATION_STREAM_STDOUT, log
            )
        )
        stderr = asyncio.create_task(
            self._record_initialization_output(
                process.stderr_byte_chunks(), attempt, runner_pb2.INITIALIZATION_STREAM_STDERR, log
            )
        )
        with suppress(BrokenPipeError, ConnectionResetError):
            # A script may exit before consuming all of its stdin.
            await process.write(source)
        await process.close_stdin()
        exit_code = await process.wait()
        await asyncio.gather(stdout, stderr)
        log.append_result(attempt, exit_code)

    @staticmethod
    async def _record_initialization_output(
        stream: AsyncIterator[bytes], attempt: int, source: runner_pb2.InitializationStream, log: InitializationLog
    ) -> None:
        async for data in stream:
            log.append_output(attempt, source, data)

    async def startup(self) -> None:
        """Load every stored session and record what the previous runner process took with it."""
        for session_id in self.store.session_ids():
            session = await self._load(session_id)
            await session.recover_after_restart()

    async def _load(self, session_id: str) -> Session:
        async with self._sessions_lock:
            if session_id not in self.sessions:
                record = self.store.read(session_id)
                try:
                    self._validate_workspace(record.cwd)
                except ValueError as error:
                    raise OpenError(f"stored session {session_id}: {error}") from error
                journal = await self._resources.enter_async_context(
                    Journal.open(self.store.directory(session_id) / "journal.sqlite", str(record.event_source_id))
                )
                self.sessions[session_id] = Session(
                    session_id,
                    record=record,
                    journal=journal,
                    store=self.store,
                    config=self.config,
                    state_owner_descriptor=self._state_owner.descriptor,
                    make_adapter=make_adapter,
                )
                await self.sessions[session_id].load_setup_state()
                for loaded in self.session_loaded:
                    loaded(self.sessions[session_id])
            return self.sessions[session_id]

    async def stored_session(self, session_id: str) -> Session | None:
        """The session if this runner holds it. Never creates one or starts its harness."""
        try:
            session_id = validate_session_id(session_id)
        except ValueError:
            return None
        return await self._load(session_id) if self.store.exists(session_id) else None

    async def open(self, request: runner_pb2.Open) -> Session:
        """Attach's Open. Without a spec it only finds the session; with one it creates the session
        or confirms it, then starts its harness if stopped."""
        session_id = self._valid_session_id(request.session_id)
        if not request.HasField("spec"):
            if not self.store.exists(session_id):
                raise SessionUnknownError(f"session {session_id} does not exist and Open carries no spec")
            return await self._load(session_id)
        self._refuse_sealed()
        setup_script = request.setup_script if request.HasField("setup_script") else None
        async with self._create_lock:
            existing = self.store.exists(session_id)
            session = await (self._confirm if existing else self._create)(session_id, request.spec, setup_script)
        await self._start(session)
        return session

    async def open_session(self, session_id: str, spec: runner_pb2.SessionSpec, setup_script: str | None) -> Session:
        """Create the session with `spec` and start it, or confirm an existing one with the same spec
        without starting anything: unlike Attach's Open, this never restarts a stopped harness."""
        session_id = self._valid_session_id(session_id)
        self._refuse_sealed()
        async with self._create_lock:
            if self.store.exists(session_id):
                return await self._confirm(session_id, spec, setup_script)
            session = await self._create(session_id, spec, setup_script)
        await self._start(session)
        return session

    async def resume(self, session_id: str) -> Session:
        """Restart a stopped harness on the session's retained spec."""
        session_id = self._valid_session_id(session_id)
        self._refuse_sealed()
        if not self.store.exists(session_id):
            raise SessionUnknownError(f"session {session_id} does not exist")
        session = await self._load(session_id)
        if session.setup_state in (runner_pb2.SETUP_STATE_FAILED, runner_pb2.SETUP_STATE_INTERRUPTED):
            raise SetupFailedError(f"session {session_id}'s setup failed or was interrupted; create a new session")
        await self._start(session)
        return session

    @staticmethod
    def _valid_session_id(session_id: str) -> str:
        try:
            return validate_session_id(session_id)
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error

    def _refuse_sealed(self) -> None:
        if self.store.sealed:
            raise SandboxSealedError("this Sandbox is sealed: its sessions can be followed, not created or resumed")

    async def _confirm(self, session_id: str, spec: runner_pb2.SessionSpec, setup_script: str | None) -> Session:
        session = await self._load(session_id)
        if spec != session.record.spec():
            raise SpecConflictError(f"session {session_id} exists with a different spec")
        if setup_script is not None:
            source = setup_script.encode()
            if len(source) > _MAX_BOOTSTRAP_BYTES:
                raise InvalidRequestError(f"setup_script exceeds {_MAX_BOOTSTRAP_BYTES} UTF-8 bytes")
            if (hashlib.sha256(source).hexdigest() if source else None) != session.record.setup_script_sha256:
                raise SpecConflictError(f"session {session_id} exists with a different setup script")
        return session

    async def _create(self, session_id: str, spec: runner_pb2.SessionSpec, setup_script: str | None) -> Session:
        """Record a new session and start its setup, if it has one; never its harness."""
        if spec.harness not in (runner_pb2.HARNESS_CLAUDE, runner_pb2.HARNESS_CODEX):
            raise InvalidRequestError("spec.harness must be CLAUDE or CODEX")
        if not spec.cwd or not spec.model:
            raise InvalidRequestError("spec.cwd and spec.model are required")
        if not PurePosixPath(spec.cwd).is_absolute():
            raise InvalidRequestError(f"spec.cwd must be an absolute path, not {spec.cwd!r}")
        try:
            self._validate_workspace(spec.cwd)
        except ValueError as error:
            raise InvalidRequestError(str(error)) from error
        script = setup_script or ""
        if len(script.encode()) > _MAX_BOOTSTRAP_BYTES:
            raise InvalidRequestError(f"setup_script exceeds {_MAX_BOOTSTRAP_BYTES} UTF-8 bytes")
        try:
            model_config = await self.config.resolve_model_config(harness=spec.harness, model=spec.model)
        except ModelConfigLookupError as error:
            raise OpenError(str(error)) from error
        record = SessionRecord.from_spec(
            spec,
            setup_script=script,
            total_context_budget_tokens=model_config.total_context_budget_tokens if model_config is not None else None,
        )
        self.store.write(session_id, record)
        session = await self._load(session_id)
        if record.setup_script_sha256 is not None:
            session.start_setup(script)
        return session

    async def _start(self, session: Session) -> None:
        """Start the harness of a session whose setup allows it; one still in setup starts when the
        setup succeeds."""
        if not session.record.context_budget_resolved:
            try:
                model_config = await self.config.resolve_model_config(
                    harness=runner_pb2.Harness.Value(session.record.harness), model=session.record.model
                )
            except ModelConfigLookupError as error:
                raise OpenError(str(error)) from error
            session.record.total_context_budget_tokens = (
                model_config.total_context_budget_tokens if model_config is not None else None
            )
            session.record.context_budget_resolved = True
            self.store.write(session.session_id, session.record)
        if self.store.sealed:
            # Sealing began meanwhile and may have listed the store before it held this session.
            await session.seal()
            raise SandboxSealedError("this Sandbox is sealed: its sessions can be followed, not created or resumed")
        if session.setup_state in (runner_pb2.SETUP_STATE_NOT_REQUIRED, runner_pb2.SETUP_STATE_SUCCEEDED):
            await session.ensure_running()

    async def seal(self) -> runner_pb2.SealResponse:
        """Sandbox teardown: refuse new and resumed sessions, stop every harness, seal every journal."""
        self.store.seal()
        sessions = [await self._load(session_id) for session_id in self.store.session_ids()]
        cursors = await asyncio.gather(*(session.seal() for session in sessions))
        return runner_pb2.SealResponse(
            sessions=[
                runner_pb2.SessionSeal(
                    session_id=session.session_id, cursor=cursor, flushed=self._flushed(session.session_id, cursor)
                )
                for session, cursor in zip(sessions, cursors, strict=True)
            ],
            pushes=self.config.ingress is not None,
        )

    def _flushed(self, session_id: str, cursor: int) -> bool:
        acknowledged = self.store.read_acknowledgement(session_id)
        return acknowledged is not None and acknowledged.sealed and acknowledged.cursor == cursor

    def _validate_workspace(self, cwd: str) -> None:
        workspace_root = self.config.workspace_root
        if workspace_root is None:
            return
        root = workspace_root.resolve()
        candidate = Path(cwd).resolve()
        if candidate == root or root not in candidate.parents:
            raise ValueError(f"spec.cwd must be a child of {root}, not {cwd!r}")

    async def stop(self) -> None:
        tasks: list[asyncio.Future[object] | asyncio.Task[None]] = [
            asyncio.ensure_future(session.stop()) for session in self.sessions.values()
        ]
        if self._initialization_task is not None:
            tasks.append(self._initialization_task)
        sessions_stopped = False
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                for task in tasks:
                    task.result()
            sessions_stopped = True
        finally:
            resources_closed = False
            try:
                await self._resources.aclose()
                resources_closed = True
            finally:
                log_closed = False
                try:
                    if self._initialization_log is not None:
                        self._initialization_log.close()
                    log_closed = True
                finally:
                    # If orderly shutdown failed, let process exit close its descriptor. The
                    # native supervisor inherited that descriptor and fences its child group;
                    # explicitly unlocking here could admit a replacement too early.
                    if sessions_stopped and resources_closed and log_closed:
                        self._state_owner.close()

    def summaries(self) -> list[runner_pb2.SessionSummary]:
        return [
            runner_pb2.SessionSummary(
                session_id=session.session_id,
                spec=session.record.spec(),
                last_cursor=session.journal.last_cursor,
                harness_state=runner_pb2.HARNESS_STATE_RUNNING if session.running else runner_pb2.HARNESS_STATE_STOPPED,
                active_turn_id=session.journal.recovery_state.active_turn_id,
                setup_state=session.setup_state,
            )
            for session in sorted(self.sessions.values(), key=lambda session: session.session_id)
        ]


class RunnerService(runner_pb2_grpc.RunnerServicer):
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    async def Initialize(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: runner_pb2.InitializeRequest, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.InitializationEvent]:
        try:
            log, execution = await self.runner.initialize(request)
            if request.after_sequence > log.last_sequence:
                raise ValueError(
                    f"after_sequence {request.after_sequence} is beyond the initialization log, "
                    f"whose last sequence is {log.last_sequence}"
                )
        except InitializationConflictError as error:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
            return
        except ValueError as error:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
            return

        cursor = request.after_sequence
        while True:
            for event in log.since(cursor):
                yield event
                cursor = event.sequence
            if execution is None:
                return
            if execution.done():
                await execution
                for event in log.since(cursor):
                    yield event
                    cursor = event.sequence
                return
            await log.wait_beyond(cursor)

    async def ListSessions(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: runner_pb2.ListSessionsRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.ListSessionsResponse:
        del request, context
        return runner_pb2.ListSessionsResponse(sessions=self.runner.summaries())

    async def Seal(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request: runner_pb2.SealRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.SealResponse:
        del request, context
        return await self.runner.seal()

    async def Attach(  # noqa: N802  # gRPC names servicer methods after the RPC
        self, request_iterator: AsyncIterator[runner_pb2.ClientMessage], context: grpc.aio.ServicerContext
    ) -> AsyncIterator[runner_pb2.ServerMessage]:
        del context
        first = await anext(request_iterator, None)
        if first is None or not first.HasField("open"):
            yield runner_pb2.ServerMessage(error="the first client message must be Open")
            return
        try:
            session = await self.runner.open(first.open)
        except OpenError as error:
            yield runner_pb2.ServerMessage(error=str(error))
            return
        except Exception as error:  # the stream must report a launch failure, not hang
            logger.exception("session %s: open failed", first.open.session_id)
            yield runner_pb2.ServerMessage(error=f"open failed: {error}")
            return
        if first.open.follow.after_cursor > session.journal.last_cursor:
            yield runner_pb2.ServerMessage(
                error=f"after_cursor {first.open.follow.after_cursor} is beyond the session log, "
                f"whose last cursor is {session.journal.last_cursor}"
            )
            return
        # The published log, not the stdout reader's batch in progress, which is not durable yet.
        published = session.journal.recovery_state
        opened_cursor = published.through_cursor
        ended = not published.harness_running and not session.setup_active
        yield runner_pb2.ServerMessage(attached=build_attached(session, published))
        closing = asyncio.Event()
        failure: list[str] = []
        consumer = asyncio.create_task(
            _consume(session, request_iterator, closing, failure), name=f"{session.session_id}-commands"
        )
        cursor = first.open.follow.after_cursor
        try:
            async with aclosing(
                follow_journal(session.journal, cursor, stop=lambda: ended or closing.is_set(), closing=closing)
            ) as entries:
                async for entry in entries:
                    if ended and entry.cursor > opened_cursor and entry.event.HasField("harness_started"):
                        return
                    yield runner_pb2.ServerMessage(event_entry=entry)
                    cursor = entry.cursor
                    if entry.cursor > opened_cursor and entry.event.HasField("harness_exited"):
                        ended = True
                    if entry.event.HasField("setup_finished"):
                        ended = entry.event.setup_finished.exit_code != 0
                    if entry.event.HasField("setup_interrupted"):
                        ended = True
                    if entry.event.HasField("harness_launch_failed"):
                        ended = True
            if session.journal.sealed_cursor is not None:
                yield runner_pb2.ServerMessage(sealed=runner_pb2.Sealed(cursor=cursor))
            elif not ended and failure:
                yield runner_pb2.ServerMessage(error=failure[0])
        finally:
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


def build_attached(session: Session, published: RecoveryState) -> runner_pb2.Attached:
    """The snapshot a follow opens with, at the journal's published cursor."""
    return runner_pb2.Attached(
        session_id=session.session_id,
        spec=session.record.spec(),
        last_cursor=published.through_cursor,
        harness_state=runner_pb2.HARNESS_STATE_RUNNING if session.running else runner_pb2.HARNESS_STATE_STOPPED,
        active_turn_id=published.active_turn_id,
        setup_state=session.setup_state,
    )


async def follow_journal(
    journal: Journal, cursor: int, *, stop: Callable[[], bool] = lambda: False, closing: asyncio.Event | None = None
) -> AsyncGenerator[event_log_pb2.EventEntry]:
    """Entries after `cursor`, then live ones as they are journaled. Pages are read only as the
    consumer drains them, so a slow consumer holds at most one. Ends once caught up, if the journal
    is sealed or `stop()` holds; `closing` wakes an idle follow to check `stop()` again."""
    while True:
        for entry in await journal.since(cursor, limit=_FOLLOW_PAGE):
            yield entry
            cursor = entry.cursor
        if cursor < journal.last_cursor:
            continue
        if journal.sealed_cursor is not None or stop():
            return
        await _wake(journal, closing, cursor)


async def _wake(journal: Journal, closing: asyncio.Event | None, cursor: int) -> None:
    waits: list[asyncio.Task[Any]] = [asyncio.create_task(journal.wait_beyond(cursor))]
    if closing is not None:
        waits.append(asyncio.create_task(closing.wait()))
    try:
        done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            completed.result()
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


async def finish_command[T](command: Awaitable[T]) -> T:
    # A connection disappearing must not cancel a shared harness operation halfway through.
    task = asyncio.ensure_future(command)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


async def _consume(
    session: Session, requests: AsyncIterator[runner_pb2.ClientMessage], closing: asyncio.Event, failure: list[str]
) -> None:
    try:
        async for message in requests:
            match message.WhichOneof("message"):
                case "command":
                    await finish_command(session.command(message.command))
                case "detach":
                    return
                case other:
                    failure.append(f"unexpected client command after Open: {other}")
                    return
    except asyncio.CancelledError:
        raise
    except Exception as error:  # a command failure ends the stream with its reason, not a hang
        logger.exception("session %s: command failed", session.session_id)
        failure.append(f"command failed: {error}")
    finally:
        closing.set()


async def serve(config: RunnerConfig, *, address: str = "127.0.0.1:0") -> tuple[grpc.aio.Server, Runner, int]:
    """Start a runner and its server; the returned port is the bound one."""
    runner = Runner(config)
    server = grpc.aio.server()
    try:
        await runner.startup()
        runner_pb2_grpc.add_RunnerServicer_to_server(RunnerService(runner), server)
        port = server.add_insecure_port(address)
        await server.start()
    except BaseException:
        await server.stop(0)
        await runner.stop()
        raise
    return server, runner, port
