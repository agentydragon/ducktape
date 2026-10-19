"""One runner session: replayable evidence, native harness process, and command journal."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from agentplane.native.transport import Frame, FrameMatcher, NativeReceipt
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.journal import ActiveTurnChangedError, CommandConflictError, Journal, JournalSealedError
from agentplane.runner.model_config import ModelConfigLookupError
from agentplane.runner.observation import Observation
from agentplane.runner.recovery import observed_items, unknown_report
from agentplane.runner.store import SessionRecord, SessionStore
from agentplane.runner.workspace import ensure_workspace

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Shutdown interrupts an active turn and gives the harness this long to report its end.
_INTERRUPT_GRACE_S = 15

# Commands a sealing Session still admits and runs: they end work rather than start it.
DRAINING_OPERATIONS = {"interrupt_turn"}


class HarnessGoneError(RuntimeError):
    """The harness ended before a frame could be written to it, or while its response was awaited."""


class StaleTurnError(RuntimeError):
    """An interrupt named a turn that is not active when the runner admits it."""


@dataclass(frozen=True)
class _Request:
    matches: FrameMatcher
    reply: asyncio.Future[NativeReceipt]


class Session:
    def __init__(
        self,
        session_id: str,
        *,
        record: SessionRecord,
        journal: Journal,
        store: SessionStore,
        config: RunnerConfig,
        make_adapter: Callable[[Session], HarnessAdapter],
        state_owner_descriptor: int,
        activity_changed: Callable[[], None],
        ensure_ready: Callable[[Session], Awaitable[None]] | None = None,
    ) -> None:
        self.session_id = session_id
        # Called whenever `busy` may have changed.
        self.activity_changed = activity_changed
        self.ensure_ready_callback = ensure_ready
        self.record = record
        # Whether the harness this Session launches resumes `record.native_session_id`; set at each launch.
        self.resuming = False
        self.store = store
        self.config = config
        self.state_owner_descriptor = state_owner_descriptor
        self.make_adapter = make_adapter
        self.directory = store.directory(session_id)
        self.journal = journal
        # The writer's view, including the stdout reader's batch before it commits. What a client
        # may be told is the published log's, `journal.recovery_state`.
        self.harness_running = journal.recovery_state.harness_running
        self.active_turn_id = journal.recovery_state.active_turn_id
        # Per-process only. A restarted runner consults the durable journal and intentionally
        # tries outstanding commands again using their original ids.
        self._dispatched_commands: set[str] = set()
        self._pending_command_ids: set[str] = set()
        # Admission is serialized and durable, while native operations run afterwards. Normal
        # operations retain their admission order; controls bypass an unrelated blocked input.
        self._scheduled_commands: set[str] = set()
        self._normal_commands: deque[command_pb2.Command] = deque()
        self._normal_dispatch_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._interrupt_commands: dict[str, str] = {}
        self.process_id: str | None = None
        self._process_state = runner_pb2.PROCESS_STATE_UNSPECIFIED
        self._ready = False
        self._failure_cursor: int | None = None
        self._setup_failure_reason = ""
        self._startup_lock = asyncio.Lock()
        self._startup_task: asyncio.Task[None] | None = None
        self._activation_task: asyncio.Task[None] | None = None
        self._interrupt_target: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"{session_id}-interrupt-target", default=None
        )
        self.process: HarnessProcess | None = None
        self.adapter: HarnessAdapter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._waiters: list[_Request] = []
        # Replies the stdout reader has matched, held until the batch recording them commits.
        self._replies: list[tuple[_Request, NativeReceipt]] = []
        self._stopping = False
        # Set under the session lock, so no harness starts once sealing has begun.
        self._sealing = journal.sealed_cursor is not None
        self._tearing_down = False
        self._lock = asyncio.Lock()
        # Sealing and the durable "possibly sent" marker share this gate. A command with no
        # marker can then be failed safely; one whose marker won the gate remains uncertain.
        self._dispatch_gate = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self.setup_state = (
            runner_pb2.SETUP_STATE_RUNNING
            if record.setup_script_sha256 is not None
            else runner_pb2.SETUP_STATE_NOT_REQUIRED
        )
        self.launch_outcome = runner_pb2.LAUNCH_OUTCOME_PENDING
        self._setup_task: asyncio.Task[None] | None = None

    async def load_prefix_state(self) -> None:
        """Recover the one-shot setup result and the launch outcome from the durable prefix, not
        process memory."""
        cursor = 0
        while self.setup_state == runner_pb2.SETUP_STATE_RUNNING or (
            self.launch_outcome == runner_pb2.LAUNCH_OUTCOME_PENDING
        ):
            entries = await self.journal.since(cursor, limit=128)
            if not entries:
                break
            for entry in entries:
                cursor = entry.cursor
                self._apply_prefix(entry)
        self._pending_command_ids = {command.command_id for command in await self.journal.pending_commands()}

    def _apply_prefix(self, entry: event_log_pb2.EventEntry) -> None:
        """The setup and launch outcomes `entry` settles, each only the first time."""
        event = entry.event
        kind = event.WhichOneof("observation")
        failed = kind in ("harness_launch_failed", "harness_exited", "setup_interrupted") or (
            kind == "setup_finished" and event.setup_finished.exit_code != 0
        )
        if self.launch_outcome == runner_pb2.LAUNCH_OUTCOME_PENDING and (kind == "harness_started" or failed):
            self.launch_outcome = (
                runner_pb2.LAUNCH_OUTCOME_STARTED if kind == "harness_started" else runner_pb2.LAUNCH_OUTCOME_FAILED
            )
        if self.setup_state == runner_pb2.SETUP_STATE_RUNNING:
            if kind == "setup_finished":
                self.setup_state = (
                    runner_pb2.SETUP_STATE_SUCCEEDED
                    if event.setup_finished.exit_code == 0
                    else runner_pb2.SETUP_STATE_FAILED
                )
            elif kind == "setup_interrupted":
                self.setup_state = runner_pb2.SETUP_STATE_INTERRUPTED
        if (kind == "setup_finished" and event.setup_finished.exit_code != 0) or kind in (
            "setup_interrupted",
            "harness_launch_failed",
        ):
            self._failure_cursor = entry.cursor

    def start_setup(self, script: str, *, before_run: Callable[[], Awaitable[None]] | None = None) -> None:
        if self.setup_state != runner_pb2.SETUP_STATE_RUNNING or self._setup_task is not None:
            raise RuntimeError("session setup was already started or completed")
        self._setup_task = asyncio.create_task(
            self._run_setup(script, before_run=before_run), name=f"{self.session_id}-setup"
        )
        self.activity_changed()

    async def _run_setup(self, script: str, *, before_run: Callable[[], Awaitable[None]] | None) -> None:
        process: HarnessProcess | None = None
        readers: list[asyncio.Task[None]] = []
        try:
            if before_run is not None:
                await before_run()
            await self.emit(event_pb2.SetupStarted(), sources=[])
            isolation = self.config.process_isolation
            cwd = await ensure_workspace(
                Path(self.record.cwd),
                workspace_root=self.config.workspace_root,
                agent_uid=isolation.agent_uid if isolation is not None else None,
                agent_gid=isolation.agent_gid if isolation is not None else None,
            )
            process = HarnessProcess(
                ["/bin/sh", "-eu"],
                cwd=cwd,
                environment={**os.environ, **self.config.harness_environment},
                state_owner_descriptor=self.state_owner_descriptor,
                process_isolation=self.config.process_isolation,
            )
            await process.start()
            stdout = asyncio.create_task(self._record_setup_output(process.output_chunks(), stdout=True))
            stderr = asyncio.create_task(self._record_setup_output(process.stderr_byte_chunks(), stdout=False))
            readers = [stdout, stderr]
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.write(script.encode())
            await process.close_stdin()
            exit_code = await process.wait()
            await asyncio.gather(stdout, stderr)
            await self.emit(event_pb2.SetupFinished(exit_code=exit_code), sources=[])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._setup_failure_reason = str(error)
            logger.exception("session %s: setup interrupted", self.session_id)
            if process is not None and process.running:
                await self._stop_setup_process(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if self.setup_state == runner_pb2.SETUP_STATE_RUNNING:
                await self.emit(event_pb2.SetupInterrupted(), sources=[])
        finally:
            if process is not None and process.running:
                await self._stop_setup_process(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def _record_setup_output(self, stream: AsyncIterator[bytes], *, stdout: bool) -> None:
        async for data in stream:
            output = event_pb2.SetupOutput(stdout=data) if stdout else event_pb2.SetupOutput(stderr=data)
            await self.emit(output, sources=[])

    @staticmethod
    async def _stop_setup_process(process: HarnessProcess) -> None:
        await process.stop()

    async def emit(
        self,
        observation: Observation,
        *,
        sources: Sequence[int],
        terminal_command_ids: Sequence[str] = (),
        native_correlation: dict[str, str] | None = None,
    ) -> event_log_pb2.EventEntry:
        """Append to the log. `sources` are the Native Events the observation was derived from; one
        the runner originates has none."""
        entry = await self.journal.append(
            observation,
            sources=sources,
            terminal_command_ids=terminal_command_ids,
            native_correlation=native_correlation,
        )
        self._apply_prefix(entry)
        match entry.event.WhichOneof("observation"):
            case "harness_started":
                self.harness_running = True
            case "harness_lost" | "harness_exited":
                self.harness_running = False
            case "turn_started":
                self.active_turn_id = entry.event.turn_started.turn_id
            case "turn_completed":
                self.active_turn_id = ""
        self._scheduled_commands.difference_update(terminal_command_ids)
        self._dispatched_commands.difference_update(terminal_command_ids)
        self._pending_command_ids.difference_update(terminal_command_ids)
        kind = entry.event.WhichOneof("observation")
        if kind == "harness_exited":
            self._ready = False
            if not entry.event.harness_exited.stopped_by_runner:
                self._failure_cursor = entry.cursor
        elif kind == "harness_lost":
            self._ready = False
        elif kind == "harness_started":
            self._failure_cursor = None
        self.activity_changed()
        return entry

    async def recover_after_restart(self) -> None:
        """The runner that wrote the log is gone, and so is any harness it was running."""
        if self.setup_state == runner_pb2.SETUP_STATE_RUNNING:
            await self.emit(event_pb2.SetupInterrupted(), sources=[])
        if not self.harness_running:
            self._ready = False
            self.process_id = None
            if self.active_turn_id:
                await self._record_turn_completed(
                    self.active_turn_id,
                    event_pb2.TURN_STATUS_PROCESS_LOST,
                    "the runner restarted without a harness for the active turn",
                    sources=[],
                )
            return
        await self.emit(event_pb2.HarnessLost(), sources=[])
        self.process_id = None
        self._process_state = runner_pb2.PROCESS_STATE_UNSPECIFIED
        if self.active_turn_id:
            await self._record_turn_completed(
                self.active_turn_id,
                event_pb2.TURN_STATUS_PROCESS_LOST,
                "the runner restarted while the turn was active",
                sources=[],
            )

    async def settle_recovered_interrupts(self) -> None:
        """A Runner restart cannot wake a harness just to deliver an interruption to its lost turn."""
        async with self._lock:
            for command in await self.journal.pending_commands():
                if command.WhichOneof("operation") != "interrupt_turn":
                    continue
                target = command.interrupt_turn.turn_id
                if target == self.active_turn_id:
                    await self._record_turn_completed(
                        target,
                        event_pb2.TURN_STATUS_PROCESS_LOST,
                        "the runner restarted before it could deliver the interruption",
                        sources=[],
                    )
                await self._noop(
                    command.command_id, f"turn {target!r} is no longer active after the runner restarted", sources=[]
                )

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.running

    @property
    def ready(self) -> bool:
        """The native handshake and recovery reconciliation both completed for this process."""
        return self._ready and self.running

    @property
    def failure_cursor(self) -> int | None:
        return self._failure_cursor

    @property
    def execution_state(self) -> runner_pb2.SessionExecutionState:
        """Current Session execution state for the diagnostic Runner snapshot."""
        if self._failure_cursor is not None:
            return runner_pb2.SESSION_EXECUTION_STATE_FAILED
        if (
            self.setup_state == runner_pb2.SETUP_STATE_RUNNING
            or (self._activation_task is not None and not self._activation_task.done())
            or (not self.ready and (self.process_id is not None or self._pending_command_ids))
        ):
            return runner_pb2.SESSION_EXECUTION_STATE_PREPARING
        if self.ready:
            return runner_pb2.SESSION_EXECUTION_STATE_READY
        return runner_pb2.SESSION_EXECUTION_STATE_DORMANT

    @property
    def busy(self) -> bool:
        """A turn runs, a command is admitted and not finished, or the setup script runs."""
        return (
            bool(self.active_turn_id or self._scheduled_commands)
            or bool(self._pending_command_ids)
            or self.setup_state == runner_pb2.SETUP_STATE_RUNNING
            or (self._activation_task is not None and not self._activation_task.done())
        )

    @property
    def setup_active(self) -> bool:
        return self._setup_task is not None and not self._setup_task.done()

    async def ensure_running(self) -> None:
        async with self._startup_lock:
            if self.ready:
                return
            task = self._startup_task
            if task is None or task.done():
                task = asyncio.create_task(self._start_harness(), name=f"{self.session_id}-harness-startup")
                self._startup_task = task
        await asyncio.shield(task)

    async def _start_harness(self) -> None:
        if self._sealing:
            raise JournalSealedError("the session is sealed")
        if self.running and self.ready:
            return
        self._ready = False
        self._process_state = runner_pb2.PROCESS_STATE_STARTING
        if not self.running:
            self.process_id = None
        self.activity_changed()
        try:
            if self.running:
                await self._stop_unready_process()
                if self.running:
                    raise RuntimeError("the previous harness process could not be stopped after startup failed")
            # Neither harness persists a conversation until its first turn, so a recorded id before
            # then names nothing to resume. The adapter starts its conversation anew and its
            # handshake id replaces the recorded one.
            self.resuming = self.record.native_session_id is not None and await self.journal.has_started_turn()
            adapter = self.make_adapter(self)
            isolation = self.config.process_isolation
            cwd = await ensure_workspace(
                Path(self.record.cwd),
                workspace_root=self.config.workspace_root,
                agent_uid=isolation.agent_uid if isolation is not None else None,
                agent_gid=isolation.agent_gid if isolation is not None else None,
            )
            process = HarnessProcess(
                adapter.command(),
                cwd=cwd,
                environment=adapter.environment(),
                state_owner_descriptor=self.state_owner_descriptor,
                process_isolation=self.config.process_isolation,
            )
            await process.start()
            self.process_id = str(uuid4())
            self.process, self.adapter, self._stopping = process, adapter, False
            self._tasks = [
                asyncio.create_task(self._read_stdout(process, adapter), name=f"{self.session_id}-stdout"),
                asyncio.create_task(self._read_stderr(process), name=f"{self.session_id}-stderr"),
            ]
            try:
                native_session_id = await adapter.handshake()
            except BaseException as error:  # cleanup, then failure is durably settled by activation
                self._stopping = True
                self._process_state = runner_pb2.PROCESS_STATE_STOPPING
                await process.stop()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self.process, self.adapter = None, None
                self.process_id = None
                self._process_state = runner_pb2.PROCESS_STATE_UNSPECIFIED
                if not isinstance(error, Exception):
                    raise
                raise RuntimeError(f"harness handshake failed: {error!r}; {process.describe_exit()}") from error
            if self.record.native_session_id != native_session_id:
                self.record.native_session_id = native_session_id
                self.store.write(self.session_id, self.record)
            await self.emit(event_pb2.HarnessStarted(resumed=self.resuming, pid=process.native_pid), sources=[])
            if self.resuming:
                for turn_id in await self.journal.recovery_turns():
                    try:
                        report = await adapter.reconcile(turn_id, resumed=True)
                    except Exception as error:
                        raise RuntimeError(f"conversation recovery failed for turn {turn_id!r}: {error!r}") from error
                    await self.emit(report, sources=[])
            async with self._lock:
                await self._reconcile_commands(recovering=True)
                self._ready = True
                self._process_state = runner_pb2.PROCESS_STATE_RUNNING
                self.activity_changed()
        except BaseException:
            self._ready = False
            try:
                await self._stop_unready_process()
            except BaseException:
                logger.exception("session %s: could not stop its unready harness process", self.session_id)
            raise

    async def _stop_unready_process(self) -> None:
        """Stop a process whose handshake/recovery did not make this session ready."""
        process = self.process
        if process is None:
            return
        self._stopping = True
        self._process_state = runner_pb2.PROCESS_STATE_STOPPING
        if process.running:
            await process.stop()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if process.running:
            return
        if self.process is process:
            self.process, self.adapter = None, None
        self.process_id = None
        self._process_state = runner_pb2.PROCESS_STATE_UNSPECIFIED
        self._stopping = False

    def _ensure_activation(self) -> None:
        task = self._activation_task
        if task is None or task.done():
            self._activation_task = asyncio.create_task(self._activate(), name=f"{self.session_id}-activation")
            self._activation_task.add_done_callback(self._activation_finished)
        self.activity_changed()

    def _activation_finished(self, task: asyncio.Task[None]) -> None:
        self.activity_changed()
        if task.cancelled():
            return
        if error := task.exception():
            logger.error(
                "session %s: activation could not settle its admitted commands",
                self.session_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _activate(self) -> None:
        try:
            if self.ensure_ready_callback is None:
                await self.ensure_running()
            else:
                await self.ensure_ready_callback(self)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("session %s: admitted work could not start its harness", self.session_id)
            try:
                async with self._lock:
                    await self._settle_undispatched_commands(
                        f"harness startup failed before this command was dispatched: {error}"
                    )
                    if self.failure_cursor is None:
                        await self.emit(event_pb2.HarnessLaunchFailed(reason=str(error)), sources=[])
            except Exception:
                logger.exception("session %s: could not settle commands after startup failure", self.session_id)
                raise
        finally:
            self.activity_changed()

    def ensure_activation(self) -> None:
        """Start one shared, detached preparation attempt after command admission commits."""
        self._ensure_activation()

    async def command(self, command: command_pb2.Command) -> None:
        """Durably admit a command before arranging its potentially blocking native work."""
        async with self._lock:
            stored = await self.journal.get(command.command_id)
            if stored is not None:
                if command_pb2.Command.FromString(stored.payload) != command:
                    raise CommandConflictError("the command id was admitted for other work")
                if stored.terminal_cursor is None:
                    if command.WhichOneof("operation") == "interrupt_turn":
                        await self._reconcile_interrupt_retry(command)
                    elif self.ready:
                        await self._schedule(command)
                    else:
                        self._ensure_activation()
                return
            operation = command.WhichOneof("operation")
            if operation == "interrupt_turn":
                target = command.interrupt_turn.turn_id
                if not target:
                    raise ValueError("interrupt_turn.turn_id is required")
                if target != self.active_turn_id:
                    raise StaleTurnError(f"turn {target!r} is not active")
            try:
                await self.journal.admit(
                    command,
                    expected_active_turn_id=command.interrupt_turn.turn_id if operation == "interrupt_turn" else None,
                )
            except ActiveTurnChangedError as error:
                raise StaleTurnError(str(error)) from error
            self._pending_command_ids.add(command.command_id)
            if self.ready:
                await self._schedule(command)
            elif operation == "interrupt_turn":
                await self._reconcile_interrupt_retry(command)
            else:
                self._ensure_activation()

    async def _reconcile_interrupt_retry(self, command: command_pb2.Command) -> None:
        """Retry a durable interrupt only while its target can still receive it."""
        target = command.interrupt_turn.turn_id
        if target != self.active_turn_id:
            await self._noop(command.command_id, f"turn {target!r} is no longer active", sources=[])
            return
        if self.ready:
            await self._schedule(command)
            return
        if any(task is not None and not task.done() for task in (self._activation_task, self._startup_task)):
            # The existing startup task will reconcile this admitted command after its handshake.
            return
        await self._noop(command.command_id, "the harness is not ready to receive this interruption", sources=[])

    async def reconcile_admitted_command(self, command: command_pb2.Command) -> None:
        """Resume one durable nonterminal command after a control-stream retry."""
        async with self._lock:
            stored = await self.journal.get(command.command_id)
            if stored is None or stored.terminal_cursor is not None:
                return
            if command_pb2.Command.FromString(stored.payload) != command:
                raise CommandConflictError("the command id was admitted for other work")
            if command.WhichOneof("operation") == "interrupt_turn":
                await self._reconcile_interrupt_retry(command)
            elif self.ready:
                await self._schedule(command)
            else:
                self._ensure_activation()

    async def _reconcile_commands(self, *, recovering: bool = False) -> None:
        """Resume journaled work after this process has a fresh native harness attachment."""
        for command in await self.journal.pending_commands():
            await self._schedule(command, recovering=recovering)

    async def _schedule(self, command: command_pb2.Command, *, recovering: bool = False) -> None:
        """Arrange one admitted command's native work without extending the admission critical section."""
        command_id = command.command_id
        stored = await self.journal.get(command_id)
        if stored is not None and stored.terminal_cursor is not None:
            return
        if recovering:
            self._scheduled_commands.discard(command_id)
            self._dispatched_commands.discard(command_id)
        elif command_id in self._scheduled_commands:
            return
        self._scheduled_commands.add(command_id)
        self.activity_changed()
        operation = command.WhichOneof("operation")
        if operation in DRAINING_OPERATIONS:
            self._track_dispatch(
                asyncio.create_task(self._dispatch(command), name=f"{self.session_id}-{command_id}-control")
            )
            return
        self._normal_commands.append(command)
        if self._normal_dispatch_task is None:
            self._normal_dispatch_task = self._track_dispatch(
                asyncio.create_task(self._dispatch_normal_commands(), name=f"{self.session_id}-normal-commands")
            )

    def _track_dispatch(self, task: asyncio.Task[None]) -> asyncio.Task[None]:
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)
        return task

    async def _dispatch_normal_commands(self) -> None:
        try:
            while self._normal_commands:
                await self._dispatch(self._normal_commands.popleft())
        finally:
            self._normal_dispatch_task = None

    async def _dispatch(self, command: command_pb2.Command) -> None:
        command_id = command.command_id
        stored = await self.journal.get(command_id)
        if stored is not None and stored.terminal_cursor is not None:
            return
        if command_id in self._dispatched_commands:
            return
        if self.adapter is None or not self.running:
            return
        operation = command.WhichOneof("operation")
        if self._sealing and operation not in DRAINING_OPERATIONS:
            # Left pending: a resumed Sandbox's runner dispatches it.
            return
        if operation == "submit_input":
            text = command.submit_input.text
            if not text:
                await self._fail(command_id, "submit_input.text is required", sources=[])
                return
            if not await self._plan_dispatch(command_id):
                return
            try:
                await self._debug_checkpoint("after-dispatch-planned", command_id)
                await self.adapter.submit(command_id, text)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error), sources=[])
            return
        if operation == "change_model":
            model = command.change_model.model
            if not model:
                await self._fail(command_id, "change_model.model is required", sources=[])
                return
            if model == self.record.model:
                await self._noop(command_id, "the requested model is already active", sources=[])
                return
            try:
                model_config = await self.config.resolve_model_config(harness=self.record.harness, model=model)
            except ModelConfigLookupError as error:
                await self._fail(command_id, str(error), sources=[])
                return
            requested_window = model_config.total_context_budget_tokens if model_config is not None else None
            current_window = self.record.total_context_budget_tokens
            if current_window != requested_window:
                await self._fail(
                    command_id,
                    "This model switch changes the configured context budget, which Agentplane currently sets "
                    "only when the harness starts. Start a new session with the requested model as a workaround.",
                    sources=[],
                )
                return
            if not await self._plan_dispatch(command_id):
                return
            try:
                await self.adapter.change_model(command_id, model)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error), sources=[])
            return
        if operation == "change_reasoning_effort":
            effort = command.change_reasoning_effort.effort
            if not effort:
                await self._fail(command_id, "change_reasoning_effort.effort is required", sources=[])
                return
            if effort == self.record.reasoning_effort:
                await self._noop(command_id, "the requested reasoning effort is already active", sources=[])
                return
            if not await self._plan_dispatch(command_id):
                return
            try:
                await self.adapter.change_reasoning_effort(command_id, effort)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error), sources=[])
            return
        if operation == "interrupt_turn":
            target = command.interrupt_turn.turn_id
            if not target:
                await self._fail(command_id, "interrupt_turn.turn_id is required", sources=[])
                return
            if target != self.active_turn_id:
                await self._noop(command_id, f"turn {target!r} is no longer active", sources=[])
                return
            if not await self._plan_dispatch(command_id, native_correlation={"turn_id": target}, allow_sealing=True):
                return
            # The turn may have ended while the durable dispatch marker was committed. A Claude
            # interrupt targets the harness's current turn, so never send a stale target after that
            # boundary; Codex also receives this exact native turn id.
            if target != self.active_turn_id:
                self._dispatched_commands.discard(command_id)
                await self._noop(command_id, f"turn {target!r} ended before interruption was sent", sources=[])
                return
            self._interrupt_commands[target] = command_id
            token = self._interrupt_target.set(target)
            try:
                await self.adapter.interrupt(target)
            except StaleTurnError as error:
                self._interrupt_commands.pop(target, None)
                self._dispatched_commands.discard(command_id)
                await self._noop(command_id, str(error), sources=[])
            except (HarnessGoneError, RuntimeError) as error:
                self._interrupt_commands.pop(target, None)
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error), sources=[])
            finally:
                self._interrupt_target.reset(token)
            return
        await self._fail(command_id, f"unrecognized command operation {operation!r}", sources=[])

    async def _plan_dispatch(
        self, command_id: str, *, native_correlation: dict[str, str] | None = None, allow_sealing: bool = False
    ) -> bool:
        """Fence the durable dispatch marker against teardown and terminal evidence."""
        async with self._dispatch_gate:
            if self._sealing and not allow_sealing:
                return False
            planned = await self.journal.dispatch_planned(command_id, native_correlation=native_correlation)
            if planned:
                self._dispatched_commands.add(command_id)
            return planned

    async def _fail(self, command_id: str, reason: str, *, sources: Sequence[int]) -> None:
        observation = event_pb2.CommandFailed(command_id=command_id, reason=reason)
        await self.emit(observation, sources=sources, terminal_command_ids=[command_id])

    async def _noop(self, command_id: str, reason: str, *, sources: Sequence[int]) -> None:
        observation = event_pb2.CommandNoop(command_id=command_id, reason=reason)
        await self.emit(observation, sources=sources, terminal_command_ids=[command_id])

    async def model_changed(self, command_id: str, model: str, *, sources: Sequence[int]) -> None:
        """Record a harness's causal model-selection effect, after it has really selected it."""
        stored = await self.journal.get(command_id)
        if stored is not None and stored.terminal_cursor is not None:
            return
        previous = self.record.model
        self.record.model = model
        self.store.write(self.session_id, self.record)
        await self.emit(
            event_pb2.ModelChanged(command_id=command_id, previous_model=previous, model=model),
            sources=sources,
            terminal_command_ids=[command_id],
        )

    async def reasoning_effort_changed(self, command_id: str, effort: str, *, sources: Sequence[int]) -> None:
        """Record effort only after the native control/turn acknowledgement."""
        stored = await self.journal.get(command_id)
        if stored is not None and stored.terminal_cursor is not None:
            return
        previous = self.record.reasoning_effort
        self.record.reasoning_effort = effort
        self.store.write(self.session_id, self.record)
        await self.emit(
            event_pb2.ReasoningEffortChanged(command_id=command_id, previous_effort=previous, effort=effort),
            sources=sources,
            terminal_command_ids=[command_id],
        )

    async def confirm_user_message(
        self,
        *,
        harness_message_id: str,
        text: str,
        origin_command_ids: Sequence[str],
        turn_id: str,
        sources: Sequence[int],
    ) -> None:
        """Record one confirmed harness message, preserving all application command origins."""
        await self._debug_checkpoint("before-user-message-confirmed", origin_command_ids[0])
        observation = event_pb2.HarnessUserMessageConfirmed(
            harness_message_id=harness_message_id,
            text=text,
            origin_command_ids=list(origin_command_ids),
            turn_id=turn_id,
        )
        await self.emit(
            observation,
            sources=sources,
            terminal_command_ids=origin_command_ids,
            native_correlation={"harness_message_id": harness_message_id, "turn_id": turn_id},
        )
        await self._debug_checkpoint("after-terminal-outcome", origin_command_ids[0])

    async def _debug_checkpoint(self, name: str, command_id: str) -> None:
        """Expose one test-selected durable boundary through the normal session stream, then pause.

        This deliberately has no release path: the process-integration test kills this runner at
        the boundary. A successor loads the persisted DebugCheckpoint and does not pause again.
        """
        configured = self.config.test_debug_checkpoint
        key = (name, command_id)
        if (
            configured is None
            or key != (configured.name, configured.command_id)
            or await self.journal.reached_checkpoint(name, command_id)
        ):
            return
        await self.emit(event_pb2.DebugCheckpoint(name=name, command_id=command_id), sources=[])
        await self._commit_batch()
        await asyncio.Event().wait()

    async def turn_completed(
        self, turn_id: str, status: event_pb2.TurnStatus, error: str = "", *, sources: Sequence[int]
    ) -> None:
        """Translate one native terminal turn result and release commands waiting on it."""
        # The session lock's holder may be waiting for the journal, which the reader's batch holds.
        await self._commit_batch()
        async with self._lock:
            await self._record_turn_completed(turn_id, status, error, sources=sources)
            if status == event_pb2.TURN_STATUS_INTERRUPTED and self.adapter is not None:
                await self.emit(await self.adapter.reconcile(turn_id, resumed=False), sources=sources)
            await self._reconcile_commands()

    async def _record_turn_completed(
        self, turn_id: str, status: event_pb2.TurnStatus, error: str = "", *, sources: Sequence[int]
    ) -> None:
        interrupt_command_id = self._interrupt_commands.pop(turn_id, "")
        interrupted = status == event_pb2.TURN_STATUS_INTERRUPTED
        observation = event_pb2.TurnCompleted(
            turn_id=turn_id,
            status=status,
            error=error,
            interrupted_by_command_id=interrupt_command_id if interrupted else "",
            ended_by_teardown=interrupted and self._tearing_down and not interrupt_command_id,
        )
        await self.emit(
            observation,
            sources=sources,
            terminal_command_ids=[interrupt_command_id]
            if interrupt_command_id and status == event_pb2.TURN_STATUS_INTERRUPTED
            else [],
            native_correlation={"turn_id": turn_id},
        )
        if status == event_pb2.TURN_STATUS_PROCESS_LOST:
            await self.emit(
                unknown_report(
                    turn_id,
                    await observed_items(self.journal, turn_id),
                    "the harness exited before continuation could be inspected",
                ),
                sources=[],
            )
        if interrupt_command_id and status != event_pb2.TURN_STATUS_INTERRUPTED:
            await self._noop(
                interrupt_command_id, "the turn completed before interruption took effect", sources=sources
            )

    async def shutdown(self) -> None:
        """Stop the harness; the session stays resumable. HarnessExited is in the log on return."""
        await self._shutdown()

    async def _shutdown(self) -> None:
        async with self._shutdown_lock:
            if self.process is None or self.adapter is None or not self.running:
                return
            self._stopping = True
            self._process_state = runner_pb2.PROCESS_STATE_STOPPING
            if turn_id := self.active_turn_id:
                token = self._interrupt_target.set(turn_id)
                try:
                    # If the named turn ended during the durable native-frame write, stopping the
                    # process handles shutdown without interrupting a successor.
                    with suppress(StaleTurnError):
                        await self.adapter.interrupt(turn_id)
                finally:
                    self._interrupt_target.reset(token)
                if self.active_turn_id == turn_id:
                    turn_end = asyncio.create_task(self._await_turn_end(turn_id))
                    try:
                        await asyncio.wait_for(turn_end, timeout=_INTERRUPT_GRACE_S)
                    except TimeoutError:
                        logger.warning(
                            "session %s: the harness did not end its turn within the grace period", self.session_id
                        )
            await self.process.stop()
            await asyncio.gather(*self._tasks)

    async def _await_turn_end(self, turn_id: str) -> None:
        cursor = self.journal.last_cursor
        while self.active_turn_id == turn_id and self.running:
            await self.journal.wait_beyond(cursor)
            cursor = self.journal.last_cursor

    async def send(self, frame: BaseModel) -> None:
        """Durably record one outbound raw frame, then write it to the harness pipe."""
        if self.process is None or not self.running:
            raise HarnessGoneError("the harness is not running")
        line = frame.model_dump_json(by_alias=True)
        await self.emit(event_pb2.Native(direction=event_pb2.DIRECTION_TO_HARNESS, line=line), sources=[])
        await self._commit_batch()
        try:
            if target := self._interrupt_target.get():

                def check_interrupt_target() -> None:
                    if self.active_turn_id != target:
                        raise StaleTurnError(f"turn {target!r} ended before interruption was sent")

                await self.process.write_line(line, before_write=check_interrupt_target)
            else:
                await self.process.write_line(line)
        except (BrokenPipeError, ConnectionResetError) as error:
            # The harness died after the check above; the stdout reader reports its exit.
            raise HarnessGoneError(f"the harness closed its stdin: {error!r}") from error

    async def request(self, frame: BaseModel, *, matches: FrameMatcher, timeout_s: float = 60) -> NativeReceipt:
        """Write a frame and return the first later frame `matches` accepts, once that is committed
        together with the Events the adapter translated from it."""
        request = _Request(matches, asyncio.get_running_loop().create_future())
        self._waiters.append(request)
        try:
            await self.send(frame)
            return await asyncio.wait_for(request.reply, timeout=timeout_s)
        finally:
            self._waiters = [waiting for waiting in self._waiters if waiting is not request]
            # When the send fails on a dead harness's pipe, nothing awaits the HarnessGoneError the
            # stdout reader then sets; HarnessExited reports that exit either way.
            if request.reply.done() and not request.reply.cancelled():
                request.reply.exception()

    async def _read_stdout(self, process: HarnessProcess, adapter: HarnessAdapter) -> None:
        try:
            async for lines in process.line_batches():
                # The lines one read delivered and the Events derived from them share a transaction,
                # unless `_commit_batch` ends it early.
                async with self.journal.batch():
                    for line in lines:
                        await self._receive(line, adapter)
                self._deliver_replies()
        except OSError:
            await process.stop()
            raise
        finally:
            exit_code = await process.wait()
            await self._harness_ended(exit_code)

    async def _receive(self, line: str, adapter: HarnessAdapter) -> None:
        entry = await self.emit(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line=line), sources=[])
        try:
            frame = json.loads(line)
        except ValueError:
            logger.warning("session %s: non-JSON line on the harness stdout: %r", self.session_id, line[:200])
            return
        if not isinstance(frame, dict):
            logger.warning("session %s: non-object frame on the harness stdout", self.session_id)
            return
        try:
            await adapter.on_frame(frame, entry.origin.sequence)
        except OSError:
            raise
        except Exception:  # a frame the adapter cannot translate must not stop the reader
            logger.exception("session %s: frame %d not translated", self.session_id, entry.origin.sequence)
        # After translation, so a commit during it cannot hand over this reply before what it proves.
        self._match_replies(frame, entry.origin.sequence)

    async def _commit_batch(self) -> None:
        """In the stdout reader, commit its batch so far and hand over the replies it holds: before a
        native write, which must follow its record, and before waiting on anything a task blocked on
        the journal may hold."""
        if self.journal.batching():
            await self.journal.commit_batch()
            self._deliver_replies()

    def _deliver_replies(self) -> None:
        replies, self._replies = self._replies, []
        for request, receipt in replies:
            if not request.reply.done():
                request.reply.set_result(receipt)

    async def _harness_ended(self, exit_code: int) -> None:
        for request in [*self._waiters, *(request for request, _ in self._replies)]:
            if not request.reply.done():
                request.reply.set_exception(HarnessGoneError(f"the harness exited with {exit_code=}"))
        self._waiters, self._replies = [], []
        await self.emit(event_pb2.HarnessExited(exit_code=exit_code, stopped_by_runner=self._stopping), sources=[])
        self._ready = False
        self.process_id = None
        self._process_state = runner_pb2.PROCESS_STATE_UNSPECIFIED
        if self.active_turn_id:
            await self._record_turn_completed(
                self.active_turn_id,
                event_pb2.TURN_STATUS_PROCESS_LOST,
                f"the harness exited with {exit_code=} during the turn",
                sources=[],
            )

    def _match_replies(self, frame: Frame, sequence: int) -> None:
        """Take the requests `frame` answers, so a later frame cannot, and hold their reply until it commits."""
        waiting = []
        for request in self._waiters:
            if not request.reply.done() and request.matches(frame):
                self._replies.append((request, NativeReceipt(frame, sequence)))
            else:
                waiting.append(request)
        self._waiters = waiting

    async def _read_stderr(self, process: HarnessProcess) -> None:
        try:
            async for chunk in process.stderr_chunks():
                await self.emit(event_pb2.HarnessStderr(text=chunk), sources=[])
        except OSError:
            await process.stop()
            raise

    async def seal(self, incarnation_uid: str) -> int:
        """Sandbox teardown: interrupt the active turn, finish setup and stop the harness, then make the journal
        final at its cursor for `incarnation_uid`."""
        await self._begin_sealing()
        await self._cancel_preparation()
        await self._shutdown()
        # A command with no durable dispatch marker is known not to have crossed the native
        # boundary. Settle it before sealing so teardown does not leave accepted work busy forever.
        # Commands with a dispatch marker remain pending: their native side effect is uncertain and
        # the next incarnation must reconcile them from evidence.
        await self._settle_undispatched_commands("the Sandbox was quiesced before this command was dispatched")
        await self.stop()
        return await self.journal.seal(incarnation_uid)

    async def _begin_sealing(self) -> None:
        """Fence future dispatch markers before teardown settles known-undispatched commands."""
        async with self._dispatch_gate, self._lock:
            self._sealing = True
            self._tearing_down = True

    async def _settle_undispatched_commands(self, reason: str) -> None:
        """Finish admitted commands the journal proves never crossed the native boundary."""
        for command in await self.journal.pending_commands():
            stored = await self.journal.get(command.command_id)
            if stored is not None and not stored.dispatch_planned:
                await self._fail(command.command_id, reason, sources=[])

    async def reopen(self, incarnation_uid: str) -> None:
        """Let a Session sealed by an earlier incarnation take work again, its harness stopped."""
        async with self._lock:
            await self.journal.reopen(incarnation_uid)
            self._sealing = False
            self._tearing_down = False

    async def stop(self) -> None:
        """Runner shutdown: let setup finish, then stop the harness and record its exit."""
        await self._cancel_preparation()
        if self._setup_task is not None and not self._setup_task.done():
            # Setup can make non-transactional workspace changes. An orderly shutdown must not
            # leave those half-applied; cancellation of this waiter must not cancel the script.
            await asyncio.shield(self._setup_task)
        async with self._shutdown_lock:
            if self.process is not None and self.running:
                self._stopping = True
                await self.process.stop()
                await asyncio.gather(*self._tasks)
        tasks = [task for task in self._dispatch_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_preparation(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._activation_task, self._startup_task)
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
