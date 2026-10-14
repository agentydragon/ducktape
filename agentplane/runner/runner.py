"""The sessions one runner process owns, and the lifecycle its control stream drives them through."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, contextmanager, suppress
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Self, assert_never, cast

from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.proto import event_log_pb2, runner_control_pb2, runner_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.claude import ClaudeAdapter
from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness import Harness
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.initialization import InitializationLog
from agentplane.runner.journal import Journal, RecoveryState
from agentplane.runner.model_config import ModelConfigLookupError
from agentplane.runner.session import Session
from agentplane.runner.store import IngressAcknowledgement, SessionRecord, SessionStore, StateOwner, validate_session_id

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

_MAX_BOOTSTRAP_BYTES = 65_536
_FOLLOW_PAGE = 128


class OpenError(Exception):
    """A session operation the runner refuses."""


class SessionUnknownError(OpenError):
    pass


class SpecConflictError(OpenError):
    """The session exists with a different spec or setup script."""


class SandboxQuiescedError(OpenError):
    pass


class SandboxNotIdleError(Exception):
    pass


class SetupFailedError(OpenError):
    """The session's setup failed or was interrupted, so it never resumes."""


class InvalidRequestError(OpenError):
    """A malformed session ID, spec or setup script."""


class InitializationConflictError(Exception):
    """The sandbox already selected a different bootstrap initialization."""


class BootstrapFailedError(OpenError):
    """The Sandbox bootstrap failed or was interrupted, so no Session opens in this Sandbox."""


def make_adapter(session: Session) -> HarnessAdapter:
    match session.record.harness:
        case Harness.CLAUDE:
            if session.config.claude is None:
                raise RuntimeError("this runner is not configured for Claude sessions")
            return ClaudeAdapter(session, session.config.claude)
        case Harness.CODEX:
            if session.config.codex is None:
                raise RuntimeError("this runner is not configured for Codex sessions")
            return CodexAdapter(session, session.config.codex)
        case _:
            assert_never(session.record.harness)


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
        self._quiescing = False
        # Set whenever `facts()` may have changed; the control stream clears it before reading.
        self.facts_changed = asyncio.Event()
        # Deliveries between RPC entry and their answer count as busy before the journal knows of them.
        self._deliveries = 0
        # When the Sandbox last became idle; None while busy.
        self._idle_since: Timestamp | None = None

    def facts(self) -> runner_control_pb2.RunnerFacts:
        """What the Runner Gateway records about this runner on the connection Lease."""
        facts = runner_control_pb2.RunnerFacts()
        if self.lameduck:
            facts.quiesce_stage = self._quiesce_stage()
        if (log := self._initialization_log) is not None:
            facts.bootstrap = self._bootstrap_state(log)
        if self._idle_since is not None:
            facts.idle_since.CopyFrom(self._idle_since)
        return facts

    @contextmanager
    def delivering(self) -> Iterator[None]:
        """Marks the Sandbox busy from RPC entry until the answer, before the journal records anything."""
        self._deliveries += 1
        self.note_activity()
        try:
            yield
        finally:
            self._deliveries -= 1
            self.note_activity()

    def note_activity(self) -> None:
        """Re-evaluate whether the Sandbox is busy and publish a change. Busy: a delivery under way, the
        bootstrap running, or any Session with a turn, an unfinished command or a setup script. Terminals
        and background processes outside those are not tracked: the runner does not supervise them."""
        busy = (
            self._deliveries > 0
            or (self._initialization_log is not None and not self._initialization_log.ended)
            or any(session.busy for session in self.sessions.values())
        )
        if busy and self._idle_since is not None:
            self._idle_since = None
        elif not busy and self._idle_since is None:
            self._idle_since = Timestamp()
            self._idle_since.GetCurrentTime()
        else:
            return
        self.facts_changed.set()

    def _quiesce_stage(self) -> runner_control_pb2.RunnerFacts.QuiesceStage:
        sessions = [self.sessions[session_id] for session_id in self.store.session_ids() if session_id in self.sessions]
        if self._quiescing or any(session.journal.sealed_cursor is None for session in sessions):
            return runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        if all(self._flushed(session.session_id, cast(int, session.journal.sealed_cursor)) for session in sessions):
            return runner_control_pb2.RunnerFacts.QUIESCE_STAGE_QUIESCED
        return runner_control_pb2.RunnerFacts.QUIESCE_STAGE_FLUSHING

    @staticmethod
    def _bootstrap_state(log: InitializationLog) -> runner_control_pb2.RunnerFacts.Bootstrap:
        if not log.ended:
            return runner_control_pb2.RunnerFacts.BOOTSTRAP_RUNNING
        outcome = log.since(log.last_sequence - 1)[0]
        if outcome.WhichOneof("observation") == "interrupted":
            return runner_control_pb2.RunnerFacts.BOOTSTRAP_INTERRUPTED
        if outcome.result.exit_code == 0:
            return runner_control_pb2.RunnerFacts.BOOTSTRAP_SUCCEEDED
        return runner_control_pb2.RunnerFacts.BOOTSTRAP_FAILED

    def acknowledged(self) -> None:
        """The ingress acknowledged a journal, which may complete a quiesce."""
        self.facts_changed.set()

    async def initialize(self, script: str) -> tuple[InitializationLog, asyncio.Task[None] | None]:
        """Select one sandbox bootstrap and return its log and current execution."""
        source = script.encode()
        if not source or len(source) > _MAX_BOOTSTRAP_BYTES:
            raise ValueError(f"initialize.script must contain 1..{_MAX_BOOTSTRAP_BYTES} UTF-8 bytes")
        async with self._initialize_lock:
            log, selected_now = self._claim_initialization(source)
            self.facts_changed.set()
            self.note_activity()
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
            self.facts_changed.set()
            self.note_activity()
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
        self.facts_changed.set()
        self.note_activity()

    @staticmethod
    async def _record_initialization_output(
        stream: AsyncIterator[bytes], attempt: int, source: runner_pb2.InitializationStream, log: InitializationLog
    ) -> None:
        async for data in stream:
            log.append_output(attempt, source, data)

    async def startup(self) -> None:
        """Load every stored session and record what the previous runner process took with it, then
        start the Sandbox bootstrap if it has not run, so it never waits for a first Session."""
        for session_id in self.store.session_ids():
            session = await self._load(session_id)
            await session.recover_after_restart()
        if self.config.launch.bootstrap is not None:
            try:
                await self.initialize(self.config.launch.bootstrap)
            except InitializationConflictError:
                # Every open refuses with the same error, which is where the caller sees it.
                logger.exception("the Sandbox selected a different bootstrap script")
        self.note_activity()

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
                    activity_changed=self.note_activity,
                )
                await self.sessions[session_id].load_prefix_state()
                await self._reopen_earlier_seal(self.sessions[session_id])
                self.note_activity()
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

    async def open_session(self, session_id: str, spec: runner_pb2.SessionSpec, setup_script: str | None) -> Session:
        """Create the session with `spec` and start it once the Sandbox bootstrap has succeeded; or confirm an existing one with the same spec and start its stopped harness."""
        session_id = self._valid_session_id(session_id)
        self.refuse_work()
        launched = runner_pb2.SessionSpec()
        launched.CopyFrom(spec)
        launched.instructions = self.config.launch.instructions(session_id, spec.instructions)
        if not self.store.exists(session_id):
            await self._bootstrap()
        async with self._create_lock:
            if self.store.exists(session_id):
                session = await self._confirm(session_id, launched, setup_script)
            else:
                session = await self._create(session_id, launched, setup_script)
        await self._start(session)
        return session

    async def _bootstrap(self) -> None:
        """Wait for the Sandbox's bootstrap, which `startup` began, and refuse unless it succeeded."""
        if self.config.launch.bootstrap is None:
            return
        log, execution = await self.initialize(self.config.launch.bootstrap)
        if execution is not None:
            # The bootstrap belongs to the Sandbox: abandoning this call must not stop it.
            await asyncio.shield(execution)
        outcome = log.since(log.last_sequence - 1)[0]
        if outcome.WhichOneof("observation") == "interrupted":
            raise BootstrapFailedError("the Sandbox bootstrap was interrupted and never runs again; use a new Sandbox")
        if outcome.result.exit_code != 0:
            raise BootstrapFailedError("the Sandbox bootstrap failed and never runs again; no session was opened")

    async def resume(self, session_id: str) -> Session:
        """Restart a stopped harness on the session's retained spec."""
        session_id = self._valid_session_id(session_id)
        self.refuse_work()
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

    @property
    def lameduck(self) -> bool:
        return self.store.lameduck

    def enter_lameduck(self) -> None:
        """Durably refuse everything but draining operations, across runner restarts."""
        self.store.enter_lameduck()

    def enter_lameduck_if_idle_since(self, token: Timestamp) -> None:
        """Enter lameduck only if the runner is idle since `token`; a runner already in lameduck stays there.
        No `await` may separate the compare from the entry."""
        if self.lameduck:
            return
        if self._idle_since is None:
            raise SandboxNotIdleError("the Sandbox is busy")
        if self._idle_since.ToMicroseconds() != token.ToMicroseconds():
            raise SandboxNotIdleError("the Sandbox's idle_since differs from if_idle_since")
        self.enter_lameduck()

    def refuse_work(self) -> None:
        """Raise unless the runner still accepts new work; a sender retries after the Sandbox resumes."""
        if self.lameduck:
            raise SandboxQuiescedError("this Sandbox is quiesced: its sessions can be followed, not created or resumed")

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
            model_config = await self.config.resolve_model_config(
                harness=Harness(runner_pb2.Harness.Name(spec.harness)), model=spec.model
            )
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
        if self.lameduck:
            # Sealing began meanwhile and may have listed the store before it held this session.
            await session.seal(self.store.incarnation_uid)
            self.refuse_work()
        if session.setup_state in (runner_pb2.SETUP_STATE_NOT_REQUIRED, runner_pb2.SETUP_STATE_SUCCEEDED):
            await session.ensure_running()

    async def adopt_incarnation(self, incarnation_uid: str | None) -> None:
        """Learn the incarnation from the gateway's `Welcome`, then take back every Session an earlier
        incarnation sealed."""
        self.store.adopt_incarnation(incarnation_uid)
        for session in list(self.sessions.values()):
            await self._reopen_earlier_seal(session)

    async def _reopen_earlier_seal(self, session: Session) -> None:
        """Reopen a journal sealed by an incarnation other than this one, and have the ingress continue it.
        A seal that names no incarnation is an earlier one's. Nothing is reopened before the incarnation is known,
        nor while this incarnation's own lameduck stands."""
        incarnation_uid = self.store.incarnation_uid
        if (
            incarnation_uid is None
            or self.lameduck
            or session.journal.sealed_cursor is None
            or session.journal.sealed_incarnation_uid == incarnation_uid
        ):
            return
        await session.reopen(incarnation_uid)
        acknowledged = self.store.read_acknowledgement(session.session_id)
        self.store.write_acknowledgement(
            session.session_id,
            IngressAcknowledgement(cursor=0 if acknowledged is None else acknowledged.cursor, sealed=False),
        )
        # The pusher stopped at the earlier seal; it takes the Session up again.
        for loaded in self.session_loaded:
            loaded(session)

    async def quiesce(self) -> runner_pb2.QuiesceResponse:
        """Sandbox teardown: refuse new work, interrupt running turns, stop every harness, seal every journal."""
        self._quiescing = True
        self.enter_lameduck()
        self.facts_changed.set()
        try:
            sessions = [await self._load(session_id) for session_id in self.store.session_ids()]
            cursors = await asyncio.gather(*(session.seal(self.store.incarnation_uid) for session in sessions))
        finally:
            self._quiescing = False
            self.facts_changed.set()
        return runner_pb2.QuiesceResponse(
            sessions=[
                runner_pb2.SessionSeal(
                    session_id=session.session_id, cursor=cursor, flushed=self._flushed(session.session_id, cursor)
                )
                for session, cursor in zip(sessions, cursors, strict=True)
            ]
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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

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
                launch_outcome=session.launch_outcome,
            )
            for session in sorted(self.sessions.values(), key=lambda session: session.session_id)
        ]


def build_attached(session: Session, published: RecoveryState) -> runner_pb2.Attached:
    """The snapshot a follow opens with, at the journal's published cursor."""
    return runner_pb2.Attached(
        session_id=session.session_id,
        spec=session.record.spec(),
        last_cursor=published.through_cursor,
        harness_state=runner_pb2.HARNESS_STATE_RUNNING if session.running else runner_pb2.HARNESS_STATE_STOPPED,
        active_turn_id=published.active_turn_id,
        setup_state=session.setup_state,
        launch_outcome=session.launch_outcome,
        incarnation_uid=session.store.incarnation_uid,
    )


async def follow_journal(journal: Journal, cursor: int) -> AsyncGenerator[event_log_pb2.EventEntry]:
    """Entries after `cursor`, then live ones as they are journaled. Pages are read only as the
    consumer drains them, so a slow consumer holds at most one. Ends once a sealed journal is read to
    its seal."""
    while True:
        for entry in await journal.since(cursor, limit=_FOLLOW_PAGE):
            yield entry
            cursor = entry.cursor
        if cursor < journal.last_cursor:
            continue
        if journal.sealed_cursor is not None:
            return
        await journal.wait_beyond(cursor)


async def finish_command[T](command: Awaitable[T]) -> T:
    # A connection disappearing must not cancel a shared harness operation halfway through.
    task = asyncio.ensure_future(command)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
