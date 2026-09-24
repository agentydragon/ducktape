"""One runner session: replayable evidence, native harness process, and command journal."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from agentplane.native.transport import Frame, FrameMatcher, NativeReceipt
from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.journal import Journal
from agentplane.runner.observation import Observation
from agentplane.runner.store import SessionRecord, SessionStore

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)

# Shutdown interrupts an active turn and gives the harness this long to report its end.
_INTERRUPT_GRACE_S = 15


class HarnessGoneError(RuntimeError):
    """The harness ended before a frame could be written to it, or while its response was awaited."""


@dataclass
class _ReplyHandling:
    """An `ordered_reply` block, whose end the stdout reader awaits once it has handed over the reply."""

    done: asyncio.Event = field(default_factory=asyncio.Event)
    requested: bool = False


@dataclass(frozen=True)
class _Request:
    matches: FrameMatcher
    reply: asyncio.Future[NativeReceipt]
    handling: _ReplyHandling | None


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
    ) -> None:
        self.session_id = session_id
        self.record = record
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
        # Admission is serialized and durable, while native operations run afterwards. Normal
        # operations retain their admission order; controls bypass an unrelated blocked input.
        self._scheduled_commands: set[str] = set()
        self._normal_commands: deque[command_pb2.Command] = deque()
        self._normal_dispatch_task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._interrupt_commands: dict[str, str] = {}
        self._stop_command_id = ""
        self.process: HarnessProcess | None = None
        self.adapter: HarnessAdapter | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._waiters: list[_Request] = []
        # Replies the stdout reader has matched, held until the batch recording them commits.
        self._replies: list[tuple[_Request, NativeReceipt]] = []
        self._translating: ContextVar[int] = ContextVar("native_source", default=0)
        self._reply_handling: ContextVar[_ReplyHandling | None] = ContextVar("reply_handling", default=None)
        self._stopping = False
        self._lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()

    async def emit(
        self,
        observation: Observation,
        *,
        sources: Sequence[int] | None = None,
        terminal_command_ids: Sequence[str] = (),
        native_correlation: dict[str, str] | None = None,
    ) -> event_log_pb2.EventEntry:
        """Append to the log. Inside frame translation, the frame's Native event is the default source."""
        if sources is None:
            sources = [source] if (source := self._translating.get()) else []
        entry = await self.journal.append(
            observation,
            sources=sources,
            terminal_command_ids=terminal_command_ids,
            native_correlation=native_correlation,
        )
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
        return entry

    async def recover_after_restart(self) -> None:
        """The runner that wrote the log is gone, and so is any harness it was running."""
        if not self.harness_running:
            return
        await self.emit(event_pb2.HarnessLost())
        if self.active_turn_id:
            await self._record_turn_completed(
                self.active_turn_id,
                event_pb2.TURN_STATUS_PROCESS_LOST,
                "the runner restarted while the turn was active",
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
            process = HarnessProcess(
                adapter.command(),
                cwd=cwd,
                environment=adapter.environment(),
                state_owner_descriptor=self.state_owner_descriptor,
            )
            await process.start()
            self.process, self.adapter, self._stopping = process, adapter, False
            self._tasks = [
                asyncio.create_task(self._read_stdout(process, adapter), name=f"{self.session_id}-stdout"),
                asyncio.create_task(self._read_stderr(process), name=f"{self.session_id}-stderr"),
            ]
            resumed = self.record.native_session_id is not None
            try:
                native_session_id = await adapter.handshake()
            except BaseException as error:  # cleanup, then the failure reaches Open
                self._stopping = True
                await process.stop()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                self.process, self.adapter = None, None
                if not isinstance(error, Exception):
                    raise
                # A harness that died mid-handshake surfaces here as HarnessGoneError; its exit says why.
                raise RuntimeError(f"harness handshake failed: {error!r}; {process.describe_exit()}") from error
            if self.record.native_session_id != native_session_id:
                self.record.native_session_id = native_session_id
                self.store.write(self.session_id, self.record)
            await self.emit(event_pb2.HarnessStarted(resumed=resumed, pid=process.native_pid), sources=[])
            await self._reconcile_commands(recovering=True)

    async def command(self, command: command_pb2.Command) -> None:
        """Durably admit a command before arranging its potentially blocking native work."""
        async with self._lock:
            await self.journal.admit(command)
            await self._schedule(command)

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
        operation = command.WhichOneof("operation")
        if operation in {"interrupt_turn", "stop_runner_session"}:
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
        if operation == "submit_input":
            text = command.submit_input.text
            if not text:
                await self._fail(command_id, "submit_input.text is required")
                return
            await self.journal.dispatch_planned(command_id)
            self._dispatched_commands.add(command_id)
            try:
                await self._debug_checkpoint("after-dispatch-planned", command_id)
                await self.adapter.submit(command_id, text)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error))
            return
        if operation == "change_model":
            model = command.change_model.model
            if not model:
                await self._fail(command_id, "change_model.model is required")
                return
            if model == self.record.model:
                await self._noop(command_id, "the requested model is already active")
                return
            await self.journal.dispatch_planned(command_id)
            self._dispatched_commands.add(command_id)
            try:
                await self.adapter.change_model(command_id, model)
            except (HarnessGoneError, RuntimeError) as error:
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error))
            return
        if operation == "interrupt_turn":
            target = command.interrupt_turn.turn_id
            if not target:
                await self._fail(command_id, "interrupt_turn.turn_id is required")
                return
            if target != self.active_turn_id:
                await self._noop(command_id, f"turn {target!r} is no longer active")
                return
            await self.journal.dispatch_planned(command_id, native_correlation={"turn_id": target})
            self._dispatched_commands.add(command_id)
            self._interrupt_commands[target] = command_id
            try:
                await self.adapter.interrupt(target)
            except (HarnessGoneError, RuntimeError) as error:
                self._interrupt_commands.pop(target, None)
                self._dispatched_commands.discard(command_id)
                await self._fail(command_id, str(error))
            return
        if operation == "stop_runner_session":
            await self.journal.dispatch_planned(command_id)
            self._dispatched_commands.add(command_id)
            self._stop_command_id = command_id
            await self._shutdown()
            return
        await self._fail(command_id, f"unrecognized command operation {operation!r}")

    async def _fail(self, command_id: str, reason: str) -> None:
        observation = event_pb2.CommandFailed(command_id=command_id, reason=reason)
        await self.emit(observation, sources=[], terminal_command_ids=[command_id])

    async def _noop(self, command_id: str, reason: str) -> None:
        observation = event_pb2.CommandNoop(command_id=command_id, reason=reason)
        await self.emit(observation, sources=[], terminal_command_ids=[command_id])

    async def model_changed(self, command_id: str, model: str, *, sources: Sequence[int] | None = None) -> None:
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

    async def turn_completed(self, turn_id: str, status: event_pb2.TurnStatus, error: str = "") -> None:
        """Translate one native terminal turn result and release commands waiting on it."""
        # The session lock's holder may be waiting for the journal, which the reader's batch holds.
        await self._commit_batch()
        async with self._lock:
            await self._record_turn_completed(turn_id, status, error)
            await self._reconcile_commands()

    async def _record_turn_completed(self, turn_id: str, status: event_pb2.TurnStatus, error: str = "") -> None:
        interrupt_command_id = self._interrupt_commands.pop(turn_id, "")
        observation = event_pb2.TurnCompleted(
            turn_id=turn_id,
            status=status,
            error=error,
            interrupted_by_command_id=(interrupt_command_id if status == event_pb2.TURN_STATUS_INTERRUPTED else ""),
        )
        await self.emit(
            observation,
            terminal_command_ids=[interrupt_command_id]
            if interrupt_command_id and status == event_pb2.TURN_STATUS_INTERRUPTED
            else [],
            native_correlation={"turn_id": turn_id},
        )
        if interrupt_command_id and status != event_pb2.TURN_STATUS_INTERRUPTED:
            await self._noop(interrupt_command_id, "the turn completed before interruption took effect")

    async def shutdown(self) -> None:
        """Stop the harness; the session stays resumable. HarnessExited is in the log on return."""
        await self._shutdown()

    async def _shutdown(self) -> None:
        async with self._shutdown_lock:
            if self.process is None or self.adapter is None or not self.running:
                if self._stop_command_id:
                    await self._noop(self._stop_command_id, "the harness is already stopped")
                    self._stop_command_id = ""
                return
            self._stopping = True
            if turn_id := self.active_turn_id:
                await self.adapter.interrupt(turn_id)
                turn_end = asyncio.create_task(self._await_turn_end())
                try:
                    await asyncio.wait_for(turn_end, timeout=_INTERRUPT_GRACE_S)
                except TimeoutError:
                    logger.warning(
                        "session %s: the harness did not end its turn within the grace period", self.session_id
                    )
            await self.process.stop()
            await asyncio.gather(*self._tasks)

    async def _await_turn_end(self) -> None:
        cursor = self.journal.last_cursor
        while self.active_turn_id and self.running:
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
            await self.process.write_line(line)
        except (BrokenPipeError, ConnectionResetError) as error:
            # The harness died after the check above; the stdout reader reports its exit.
            raise HarnessGoneError(f"the harness closed its stdin: {error!r}") from error

    async def request(self, frame: BaseModel, *, matches: FrameMatcher, timeout_s: float = 60) -> NativeReceipt:
        """Write a frame and return the first later frame `matches` accepts, once that is committed."""
        if (handling := self._reply_handling.get()) is not None:
            if handling.requested:
                raise RuntimeError("an ordered_reply block awaits one reply; the reader waits for the block's end")
            handling.requested = True
        request = _Request(matches, asyncio.get_running_loop().create_future(), handling)
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

    @asynccontextmanager
    async def ordered_reply(self) -> AsyncIterator[None]:
        """Handle the reply to this block's one `request` before the frames after it are translated.

        The stdout reader hands the reply over and waits for the block to end, so the Events the
        block derives from the reply precede those of later frames. The block only records: it must
        not wait on the harness or the session lock.
        """
        handling = _ReplyHandling()
        token = self._reply_handling.set(handling)
        try:
            yield
        finally:
            self._reply_handling.reset(token)
            handling.done.set()

    async def _read_stdout(self, process: HarnessProcess, adapter: HarnessAdapter) -> None:
        try:
            async for lines in process.line_batches():
                # The lines one read delivered and the Events derived from them share a transaction,
                # unless `_commit_batch` ends it early.
                async with self.journal.batch():
                    for line in lines:
                        await self._receive(line, adapter)
                        if self._replies:
                            await self._commit_batch()
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
        self._match_replies(frame, entry.origin.sequence)
        token = self._translating.set(entry.origin.sequence)
        try:
            await adapter.on_frame(frame, entry.origin.sequence)
        except OSError:
            raise
        except Exception:  # a frame the adapter cannot translate must not stop the reader
            logger.exception("session %s: frame %d not translated", self.session_id, entry.origin.sequence)
        finally:
            self._translating.reset(token)

    async def _commit_batch(self) -> None:
        """In the stdout reader, commit its batch so far and hand over the replies it holds: after a
        frame that answers a request, before a native write, which must follow its record, and
        before waiting on anything a task blocked on the journal may hold."""
        if not self.journal.batching():
            return
        await self.journal.commit_batch()
        replies, self._replies = self._replies, []
        for request, receipt in replies:
            if not request.reply.done():
                request.reply.set_result(receipt)
        for request, _ in replies:
            if request.handling is not None:
                await request.handling.done.wait()

    async def _harness_ended(self, exit_code: int) -> None:
        for request in [*self._waiters, *(request for request, _ in self._replies)]:
            if not request.reply.done():
                request.reply.set_exception(HarnessGoneError(f"the harness exited with {exit_code=}"))
        self._waiters, self._replies = [], []
        stop_command_id = self._stop_command_id if self._stopping else ""
        if stop_command_id:
            observation = event_pb2.HarnessExited(
                exit_code=exit_code, stopped_by_runner=self._stopping, stopped_by_command_id=stop_command_id
            )
            self._stop_command_id = ""
        else:
            observation = event_pb2.HarnessExited(
                exit_code=exit_code, stopped_by_runner=self._stopping, stopped_by_command_id=""
            )
        await self.emit(observation, sources=[], terminal_command_ids=[stop_command_id] if stop_command_id else [])
        if self.active_turn_id:
            await self._record_turn_completed(
                self.active_turn_id,
                event_pb2.TURN_STATUS_PROCESS_LOST,
                f"the harness exited with {exit_code=} during the turn",
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

    async def stop(self) -> None:
        """Runner shutdown: stop the harness without interrupting; the log records the exit."""
        async with self._shutdown_lock:
            if self.process is not None and self.running:
                self._stopping = True
                await self.process.stop()
                await asyncio.gather(*self._tasks)
        tasks = [task for task in self._dispatch_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
