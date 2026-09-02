"""One session: its log, its harness process, and the state both attachments and adapters read.

State that the protocol exposes (`harness_running`, `active_turn_id`, unsettled and settled inputs)
is derived from the log by `_apply`, on load and on every append, so a restarted runner reasons
from the same facts a live one does.
"""

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


class Attachment:
    """The stream currently controlling a session; a newer Open supersedes it."""

    def __init__(self) -> None:
        self.superseded = asyncio.Event()


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
        self.log = EventLog(self.directory / "events.jsonl")
        self.harness_running = False
        self.active_turn_id = ""
        self.unsettled_inputs: list[str] = []
        self.settled_inputs: dict[str, pb.Event] = {}
        for event in self.log.events:
            self._apply(event)
        self.process: HarnessProcess | None = None
        self.adapter: HarnessAdapter | None = None
        self.attachment: Attachment | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._waiters: list[tuple[Callable[[Frame], bool], asyncio.Future[NativeFrame]]] = []
        self._translating = 0
        self._stopping = False
        self._lock = asyncio.Lock()

    def _apply(self, event: pb.Event) -> None:
        match event.WhichOneof("observation"):
            case "harness_started":
                self.harness_running = True
            case "harness_exited" | "harness_lost":
                self.harness_running = False
            case "turn_started":
                self.active_turn_id = event.turn_started.turn_id
            case "turn_completed":
                self.active_turn_id = ""
            case "input_submitted":
                self.unsettled_inputs.append(event.input_submitted.input_id)
            case "input_accepted":
                self._settle(event.input_accepted.input_id, event)
            case "input_rejected":
                self._settle(event.input_rejected.input_id, event)
            case "input_uncertain":
                self._settle(event.input_uncertain.input_id, event)

    def _settle(self, input_id: str, event: pb.Event) -> None:
        if input_id in self.unsettled_inputs:
            self.unsettled_inputs.remove(input_id)
        self.settled_inputs[input_id] = event

    def emit(self, observation: Observation, *, sources: Sequence[int] | None = None) -> pb.Event:
        """Append to the log. Inside frame translation, the frame's Native event is the default source."""
        if sources is None:
            sources = [self._translating] if self._translating else []
        event = self.log.append(observation, sources=sources)
        self._apply(event)
        return event

    def recover_after_restart(self) -> None:
        """The runner that wrote the log is gone, and so is any harness it was running."""
        if not self.harness_running:
            return
        self.emit(pb.HarnessLost())
        if self.active_turn_id:
            self.emit(
                pb.TurnCompleted(
                    turn_id=self.active_turn_id,
                    status=pb.TURN_STATUS_PROCESS_LOST,
                    error="the runner restarted while the turn was active",
                )
            )
        for input_id in list(self.unsettled_inputs):
            self.emit(pb.InputUncertain(input_id=input_id))

    def attach(self) -> Attachment:
        if self.attachment is not None:
            self.attachment.superseded.set()
        self.attachment = Attachment()
        return self.attachment

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

    async def submit(self, input_id: str, text: str) -> None:
        async with self._lock:
            if (settled := self.settled_inputs.get(input_id)) is not None:
                # A retry after a lost connection: the outcome is re-reported, never re-delivered.
                self.emit(_settlement(settled), sources=[])
                return
            if input_id in self.unsettled_inputs:
                return
            if self.adapter is None or not self.running:
                self.emit(pb.InputRejected(input_id=input_id, reason="the harness is not running"), sources=[])
                return
            await self.adapter.submit(input_id, text)

    async def interrupt(self) -> None:
        async with self._lock:
            if self.adapter is not None and self.running and self.active_turn_id:
                await self.adapter.interrupt()

    async def shutdown(self) -> None:
        """Stop the harness; the session stays resumable. HarnessExited is in the log on return."""
        async with self._lock:
            if self.process is None or self.adapter is None or not self.running:
                return
            self._stopping = True
            if self.active_turn_id:
                await self.adapter.interrupt()
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
                    await adapter.on_frame(frame)
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
        self.emit(pb.HarnessExited(exit_code=exit_code, stopped_by_runner=self._stopping), sources=[])
        if self.active_turn_id:
            self.emit(
                pb.TurnCompleted(
                    turn_id=self.active_turn_id,
                    status=pb.TURN_STATUS_PROCESS_LOST,
                    error=f"the harness exited with {exit_code=} during the turn",
                ),
                sources=[],
            )
        for input_id in list(self.unsettled_inputs):
            self.emit(pb.InputUncertain(input_id=input_id), sources=[])

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


def _settlement(event: pb.Event) -> Observation:
    match event.WhichOneof("observation"):
        case "input_accepted":
            return pb.InputAccepted(input_id=event.input_accepted.input_id, turn_id=event.input_accepted.turn_id)
        case "input_rejected":
            return pb.InputRejected(input_id=event.input_rejected.input_id, reason=event.input_rejected.reason)
        case "input_uncertain":
            return pb.InputUncertain(input_id=event.input_uncertain.input_id)
    raise ValueError(f"not an input settlement: {event.sequence=}")
