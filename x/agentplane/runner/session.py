"""One runner session: replayable evidence, native harness process, and command journal."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.adapter import HarnessAdapter
from x.agentplane.runner.command_journal import CommandConflictError, CommandJournal
from x.agentplane.runner.config import RunnerConfig
from x.agentplane.runner.event_log import EventLog, Observation
from x.agentplane.runner.harness_process import HarnessProcess
from x.agentplane.runner.store import SessionRecord, SessionStore

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

Frame = dict[str, Any]

# Shutdown interrupts an active turn and gives the harness this long to report its end.
_INTERRUPT_GRACE_S = 15


@dataclass(frozen=True)
class NativeFrame:
    frame: Frame
    # The Native event's sequence, for derived events to cite.
    sequence: int


class HarnessGoneError(RuntimeError):
    """The harness ended while a native response was still awaited."""


class Session:
    def __init__(
        self,
        session_id: str,
        *,
        record: SessionRecord,
        store: SessionStore,
        config: RunnerConfig,
        make_adapter: Callable[[Session], HarnessAdapter],
    ) -> None:
        self.session_id = session_id
        self.record = record
        self.store = store
        self.config = config
        self.make_adapter = make_adapter
        self.directory = store.directory(session_id)
        self.log = EventLog(self.directory / "events-v2.jsonl")
        self.journal = CommandJournal(self.directory / "commands-v2.jsonl")
        self.harness_running = False
        self.active_turn_id = ""
        self.received_commands: set[str] = set()
        self.terminal_commands: set[str] = set()
        # Per-process only. A restarted runner consults the durable journal and intentionally
        # tries outstanding commands again using their original ids.
        self._dispatched_commands: set[str] = set()
        self._interrupt_commands: dict[str, str] = {}
        self._stop_command_id = ""
        self._debug_checkpoints_reached: set[tuple[str, str]] = set()
        for event in self.log.events:
            self._apply(event)
        self.process: HarnessProcess | None = None
        self.adapter: HarnessAdapter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._waiters: list[tuple[Callable[[Frame], bool], asyncio.Future[NativeFrame]]] = []
        self._translating = 0
        self._stopping = False
        self._lock = asyncio.Lock()

    def _apply(self, event: pb.Event) -> None:
        match event.WhichOneof("observation"):
            case "harness_started":
                self.harness_running = True
            case "harness_lost":
                self.harness_running = False
            case "harness_exited":
                self.harness_running = False
                if event.harness_exited.stopped_by_command_id:
                    self.terminal_commands.add(event.harness_exited.stopped_by_command_id)
            case "turn_started":
                self.active_turn_id = event.turn_started.turn_id
            case "turn_completed":
                self.active_turn_id = ""
                if event.turn_completed.interrupted_by_command_id:
                    self.terminal_commands.add(event.turn_completed.interrupted_by_command_id)
            case "command_received":
                self.received_commands.add(event.command_received.command_id)
            case "command_rejected":
                self.terminal_commands.add(event.command_rejected.command_id)
            case "command_noop":
                self.terminal_commands.add(event.command_noop.command_id)
            case "harness_user_message_confirmed":
                self.terminal_commands.update(event.harness_user_message_confirmed.origin_command_ids)
            case "model_changed":
                self.terminal_commands.add(event.model_changed.command_id)
            case "debug_checkpoint":
                self._debug_checkpoints_reached.add((event.debug_checkpoint.name, event.debug_checkpoint.command_id))

    def emit(self, observation: Observation, *, sources: Sequence[int] | None = None) -> pb.Event:
        """Append to the log. Inside frame translation, the frame's Native event is the default source."""
        if sources is None:
            sources = [self._translating] if self._translating else []
        event = self.log.append(observation, sources=sources)
        self._apply(event)
        return event

    def recover_after_restart(self) -> None:
        """The runner that wrote the log is gone, and so is any harness it was running."""
        # A crash between journal commit and public event must not make the app forget a command.
        # The same durable receipt is replayed before any later reconciliation attempt.
        for entry in self.journal.entries:
            if entry.command.command_id not in self.received_commands:
                self.emit(pb.CommandReceived(command_id=entry.command.command_id), sources=[])
            if entry.state == "terminal" and entry.command.command_id not in self.terminal_commands:
                if entry.outcome is None:  # CommandJournal rejects this on load; keep recovery defensive.
                    raise ValueError(f"terminal command {entry.command.command_id!r} has no durable outcome")
                self.emit(entry.outcome, sources=[])
        if not self.harness_running:
            return
        self.emit(pb.HarnessLost())
        if self.active_turn_id:
            self._record_turn_completed(
                self.active_turn_id, pb.TURN_STATUS_PROCESS_LOST, "the runner restarted while the turn was active"
            )

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.running

    async def ensure_running(self) -> None:
        async with self._lock:
            if self.running:
                return
            adapter = self.make_adapter(self)
            # The session's working directory is the spec's; a fresh one is created for the harness.
            cwd = Path(self.record.cwd)
            await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
            process = HarnessProcess(adapter.command(), cwd=cwd, environment=adapter.environment())
            await process.start()
            self.process, self.adapter, self._stopping = process, adapter, False
            self._tasks = [
                asyncio.create_task(self._read_stdout(process, adapter), name=f"{self.session_id}-stdout"),
                asyncio.create_task(self._read_stderr(process), name=f"{self.session_id}-stderr"),
            ]
            resumed = self.record.native_session_id is not None
            try:
                native_session_id = await adapter.handshake()
            except BaseException:  # cleanup, then the failure reaches Open
                self._stopping = True
                await process.stop()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self.process, self.adapter = None, None
                raise
            if self.record.native_session_id != native_session_id:
                self.record.native_session_id = native_session_id
                self.store.write(self.session_id, self.record)
            self.emit(pb.HarnessStarted(resumed=resumed, pid=process.process.pid), sources=[])
            await self._reconcile_commands(recovering=True)

    async def command(self, command: pb.Command) -> None:
        async with self._lock:
            if not command.command_id or command.WhichOneof("operation") is None:
                return
            try:
                received = self.journal.receive(command)
            except CommandConflictError as error:
                self.emit(pb.CommandRejected(command_id=command.command_id, reason=str(error)), sources=[])
                return
            if received or command.command_id not in self.received_commands:
                self.emit(pb.CommandReceived(command_id=command.command_id), sources=[])
            await self._dispatch(command)

    async def _reconcile_commands(self, *, recovering: bool = False) -> None:
        """Resume journaled work after this process has a fresh native harness attachment."""
        for entry in self.journal.entries:
            if entry.command.command_id not in self.terminal_commands:
                await self._dispatch(entry.command, recovering=recovering)

    async def _dispatch(self, command: pb.Command, *, recovering: bool = False) -> None:
        command_id = command.command_id
        entry = self.journal.get(command_id)
        if entry is None or command_id in self.terminal_commands:
            return
        if command_id in self._dispatched_commands and not recovering:
            return
        if recovering:
            self._dispatched_commands.discard(command_id)
        if self.adapter is None or not self.running:
            return
        operation = command.WhichOneof("operation")
        if operation == "submit_input":
            text = command.submit_input.text
            if not text:
                self._reject(command_id, "submit_input.text is required")
                return
            self.journal.dispatch_planned(command_id)
            self._dispatched_commands.add(command_id)
            try:
                await self._debug_checkpoint("after-dispatch-planned", command_id)
                await self.adapter.submit(command_id, text)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                self._reject(command_id, str(error))
            return
        if operation == "change_model":
            model = command.change_model.model
            if not model:
                self._reject(command_id, "change_model.model is required")
                return
            if model == self.record.model:
                self._noop(command_id, "the requested model is already active")
                return
            self.journal.dispatch_planned(command_id)
            self._dispatched_commands.add(command_id)
            try:
                await self.adapter.change_model(command_id, model)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                self._reject(command_id, str(error))
            return
        if operation == "interrupt_turn":
            target = command.interrupt_turn.turn_id
            if not target:
                self._reject(command_id, "interrupt_turn.turn_id is required")
                return
            if target != self.active_turn_id:
                self._noop(command_id, f"turn {target!r} is no longer active")
                return
            self.journal.dispatch_planned(command_id, native_correlation={"turn_id": target})
            self._dispatched_commands.add(command_id)
            self._interrupt_commands[target] = command_id
            try:
                await self.adapter.interrupt()
            except (HarnessGoneError, RuntimeError) as error:
                self._interrupt_commands.pop(target, None)
                self._dispatched_commands.discard(command_id)
                self._reject(command_id, str(error))
            return
        if operation == "stop_runner_session":
            self.journal.dispatch_planned(command_id)
            self._dispatched_commands.add(command_id)
            self._stop_command_id = command_id
            await self._shutdown_locked()
            return
        self._reject(command_id, f"unrecognized command operation {operation!r}")

    def _effect(self, command_id: str, observation: Observation, *, sources: Sequence[int] | None = None) -> None:
        self.journal.native_effect_observed(command_id)
        self.journal.terminal(command_id, outcome=observation)
        self.emit(observation, sources=sources)

    def _reject(self, command_id: str, reason: str) -> None:
        observation = pb.CommandRejected(command_id=command_id, reason=reason)
        self.journal.terminal(command_id, outcome=observation)
        self.emit(observation, sources=[])

    def _noop(self, command_id: str, reason: str) -> None:
        observation = pb.CommandNoop(command_id=command_id, reason=reason)
        self.journal.terminal(command_id, outcome=observation)
        self.emit(observation, sources=[])

    def model_changed(self, command_id: str, model: str, *, sources: Sequence[int] | None = None) -> None:
        """Record a harness's causal model-selection effect, after it has really selected it."""
        if command_id in self.terminal_commands:
            return
        previous = self.record.model
        self.record.model = model
        self.store.write(self.session_id, self.record)
        self._effect(
            command_id, pb.ModelChanged(command_id=command_id, previous_model=previous, model=model), sources=sources
        )

    async def confirm_user_message(
        self,
        *,
        harness_message_id: str,
        text: str,
        origin_command_ids: Sequence[str],
        turn_id: str,
        sources: Sequence[int] | None = None,
    ) -> None:
        """Record one confirmed harness message, preserving all application command origins."""
        observation = pb.HarnessUserMessageConfirmed(
            harness_message_id=harness_message_id,
            text=text,
            origin_command_ids=list(origin_command_ids),
            turn_id=turn_id,
        )
        for command_id in origin_command_ids:
            self.journal.native_effect_observed(
                command_id, native_correlation={"harness_message_id": harness_message_id, "turn_id": turn_id}
            )
            self.journal.terminal(command_id, outcome=observation)
        # A real-process crash test pauses here: durable terminal evidence exists, but its public
        # replayable Event has not been appended yet. Recovery must append this exact observation.
        await self._debug_checkpoint("after-terminal-outcome", origin_command_ids[0])
        self.emit(observation, sources=sources)

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
            or key in self._debug_checkpoints_reached
        ):
            return
        self.emit(pb.DebugCheckpoint(name=name, command_id=command_id), sources=[])
        await asyncio.Event().wait()

    async def turn_completed(self, turn_id: str, status: pb.TurnStatus.ValueType, error: str = "") -> None:
        """Translate one native terminal turn result and release commands waiting on it."""
        async with self._lock:
            self._record_turn_completed(turn_id, status, error)
            await self._reconcile_commands()

    def _record_turn_completed(self, turn_id: str, status: pb.TurnStatus.ValueType, error: str = "") -> None:
        interrupt_command_id = self._interrupt_commands.pop(turn_id, "")
        observation = pb.TurnCompleted(
            turn_id=turn_id,
            status=status,
            error=error,
            interrupted_by_command_id=(interrupt_command_id if status == pb.TURN_STATUS_INTERRUPTED else ""),
        )
        if interrupt_command_id and status == pb.TURN_STATUS_INTERRUPTED:
            self.journal.native_effect_observed(interrupt_command_id, native_correlation={"turn_id": turn_id})
            self.journal.terminal(interrupt_command_id, outcome=observation)
        self.emit(observation)
        if interrupt_command_id and status != pb.TURN_STATUS_INTERRUPTED:
            self._noop(interrupt_command_id, "the turn completed before interruption took effect")

    async def shutdown(self) -> None:
        """Stop the harness; the session stays resumable. HarnessExited is in the log on return."""
        async with self._lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        if self.process is None or self.adapter is None or not self.running:
            if self._stop_command_id:
                self._noop(self._stop_command_id, "the harness is already stopped")
                self._stop_command_id = ""
            return
        self._stopping = True
        if self.active_turn_id:
            await self.adapter.interrupt()
            turn_end = asyncio.create_task(self._await_turn_end())
            try:
                await asyncio.wait_for(turn_end, timeout=_INTERRUPT_GRACE_S)
            except TimeoutError:
                logger.warning("session %s: the harness did not end its turn within the grace period", self.session_id)
        await self.process.stop()
        await asyncio.gather(*self._tasks)

    async def _await_turn_end(self) -> None:
        cursor = self.log.last_sequence
        while self.active_turn_id and self.running:
            await self.log.wait_beyond(cursor)
            cursor = self.log.last_sequence

    async def write_native(self, frame: BaseModel) -> None:
        if self.process is None or not self.running:
            raise HarnessGoneError("the harness is not running")
        line = frame.model_dump_json(by_alias=True)
        self.emit(pb.Native(direction=pb.DIRECTION_TO_HARNESS, line=line), sources=[])
        await self.process.write_line(line)

    async def request(
        self, frame: BaseModel, *, matches: Callable[[Frame], bool], timeout_s: float = 60
    ) -> NativeFrame:
        """Write a frame and return the first later frame `matches` accepts."""
        waiter: asyncio.Future[NativeFrame] = asyncio.get_running_loop().create_future()
        self._waiters.append((matches, waiter))
        try:
            await self.write_native(frame)
            return await asyncio.wait_for(waiter, timeout=timeout_s)
        finally:
            self._waiters = [entry for entry in self._waiters if entry[1] is not waiter]

    async def _read_stdout(self, process: HarnessProcess, adapter: HarnessAdapter) -> None:
        try:
            async for line in process.lines():
                event = self.emit(pb.Native(direction=pb.DIRECTION_FROM_HARNESS, line=line), sources=[])
                try:
                    frame = json.loads(line)
                except ValueError:
                    logger.warning("session %s: non-JSON line on the harness stdout: %r", self.session_id, line[:200])
                    continue
                if not isinstance(frame, dict):
                    logger.warning("session %s: non-object frame on the harness stdout", self.session_id)
                    continue
                self._resolve_waiters(frame, event.sequence)
                self._translating = event.sequence
                try:
                    await adapter.on_frame(frame, event.sequence)
                except Exception:  # a frame the adapter cannot translate must not stop the reader
                    logger.exception("session %s: frame %d not translated", self.session_id, event.sequence)
                finally:
                    self._translating = 0
        finally:
            exit_code = await process.wait()
            self._harness_ended(exit_code)

    def _harness_ended(self, exit_code: int) -> None:
        for _, waiter in self._waiters:
            if not waiter.done():
                waiter.set_exception(HarnessGoneError(f"the harness exited with {exit_code=}"))
        self._waiters = []
        stop_command_id = self._stop_command_id if self._stopping else ""
        if stop_command_id:
            observation = pb.HarnessExited(
                exit_code=exit_code, stopped_by_runner=self._stopping, stopped_by_command_id=stop_command_id
            )
            self.journal.native_effect_observed(stop_command_id)
            self.journal.terminal(stop_command_id, outcome=observation)
            self._stop_command_id = ""
        else:
            observation = pb.HarnessExited(
                exit_code=exit_code, stopped_by_runner=self._stopping, stopped_by_command_id=""
            )
        self.emit(observation, sources=[])
        if self.active_turn_id:
            self._record_turn_completed(
                self.active_turn_id,
                pb.TURN_STATUS_PROCESS_LOST,
                f"the harness exited with {exit_code=} during the turn",
            )

    def _resolve_waiters(self, frame: Frame, sequence: int) -> None:
        for matches, waiter in self._waiters:
            if not waiter.done() and matches(frame):
                waiter.set_result(NativeFrame(frame, sequence))

    async def _read_stderr(self, process: HarnessProcess) -> None:
        async for chunk in process.stderr_chunks():
            self.emit(pb.HarnessStderr(text=chunk), sources=[])

    async def stop(self) -> None:
        """Runner shutdown: stop the harness without interrupting; the log records the exit."""
        async with self._lock:
            if self.process is not None and self.running:
                self._stopping = True
                await self.process.stop()
                await asyncio.gather(*self._tasks)
        self.log.close()
        self.journal.close()
