"""The session API a harness run-loop pumps through: one sequence, retention, projection, journal.

Harness-invariant once a native stream has been reduced to neutral operations. The run-loop hands
`SessionPump` the CLI's stdout frames and the console's commands; it numbers everything this end
sends on one dense sequence, retains a replay window, and folds the stream into the acknowledged
`OperationJournal` (<operation_journal.py>). `StdinWriter` serializes the line writes back to the
CLI. The console transport that carries all this is <communicator.py>; what a native frame means
is the backend's `HarnessDriver`.

**One sequence numbers everything this end sends** — stdout frames, setup narration, injected
input — minted where the event happens rather than where the socket is, so the seq the projector
stamps into provenance is the seq the recorded frame carries, and both survive the socket that
happens to be up.
"""

from __future__ import annotations

from collections import deque
from typing import Any
from uuid import UUID

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from haku.runtime.x.bridge.backend import HarnessDriver
from haku.runtime.x.bridge.claude_projection import Projected
from haku.runtime.x.bridge.neutral_operations import (
    BatchAck,
    ConsoleResume,
    Operation,
    OperationBatch,
    TurnAborted,
    TurnEnded,
    TurnOpened,
)
from haku.runtime.x.bridge.operation_journal import OperationJournal
from haku.runtime.x.bridge.protocol import (
    HarnessFrame,
    PromptDispatch,
    RunnerJournal,
    SetupOutput,
    TextWebSocket,
    encode_object,
)

# How many already-sent frames are kept to hand a console that adopts this session: a window over
# what a dying console may not have recorded, not a second copy of the rollout. Sized for a turn's
# assistant messages and tool results, which is what a roll mid-turn can strand. Journal batches
# are retained separately and unbounded, by the ACK contract (<operation_journal.py>).
REPLAY_WINDOW = 500


class StdinWriter:
    """Line writes into the CLI, serialized: the console handler and the control-refusal path both
    write, and interleaving two halves of two lines would hand the CLI garbage."""

    def __init__(self, stdin: anyio.abc.ByteSendStream):
        self._stdin = stdin
        self._lock = anyio.Lock()

    async def write_object(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            await self._stdin.send((encode_object(payload) + "\n").encode())

    async def aclose(self) -> None:
        async with self._lock:
            await self._stdin.aclose()


def _journal_text(batch: OperationBatch) -> str:
    return RunnerJournal(message=batch).model_dump_json()


class SessionPump:
    """One session's numbering, retention, projection and journaling, across every socket.

    **The number is this end's to mint**, because this end survives: the console is replaced on
    every roll while this process holds the CLI across as many sockets as that takes. Everything
    stamped goes out through one buffer under one lock, so the wire order is the stamp order; a
    reconnect replays retained frames above the console's frame cursor and retained journal
    batches above its batch cursor, and the console deduplicates both — frames by `runner_seq`,
    batches by idempotent commit.
    """

    def __init__(self, driver: HarnessDriver, outbound: MemoryObjectSendStream[str], *, window: int = REPLAY_WINDOW):
        self._driver = driver
        self._journal = OperationJournal()
        self._outbound = outbound
        self._lock = anyio.Lock()
        self._next_seq = 1
        self._retained: deque[tuple[int, str]] = deque(maxlen=window)
        # An interrupt was asked and no turn end has answered it yet. Cleared by the end it
        # rewrites, or by a turn opening — a fresh exchange means the abort's target already ended.
        self._abort_pending = False
        # Dispatch is idempotent by prompt id: the console re-dispatches unadmitted prompts after
        # a reconnect, and an id already taken is the same prompt, not a second one.
        self._taken_prompts: set[UUID] = set()

    def seed(self, resume_from: int | None) -> None:
        """Lift the counter above what the console already holds, if it holds anything.

        `max` rather than assignment: a cursor is a floor, so a counter already past it keeps
        going. Called before any narration, which is numbered too and must not land below what
        the console already recorded.
        """
        if resume_from is not None:
            self._next_seq = max(self._next_seq, resume_from + 1)

    def missed(self, resume_from: int | None) -> list[str]:
        """The retained frames a console holding *resume_from* has not been given.

        None is a console with nothing recorded; it gets the whole window. Journal replay is the
        journal's own (`resumed`); the two cursors are independent by design.
        """
        if resume_from is None:
            return [text for _, text in self._retained]
        return [text for seq, text in self._retained if seq > resume_from]

    def resumed(self, resume: ConsoleResume) -> list[str]:
        """Everything the journal owes a (re)connected console, from its durable batch cursor.

        Deliberately lock-free (as `missed`): both run between connections, where the one other
        stamper may be parked mid-`send` on a full buffer holding the lock — its already-stamped
        text is either inside the replay window (sent here, deduplicated later) or still in the
        buffer (sent once when the serve loop drains it), so nothing is lost or doubled durably.
        """
        return [_journal_text(batch) for batch in self._journal.resume(resume.acked_batch_seq)]

    async def narration(self, websocket: TextWebSocket, output: SetupOutput) -> None:
        """Number one bootstrap chunk and send it directly — the serve loop is not running yet."""
        async with self._lock:
            text, _ = self._stamp(output, retain=False)
            await websocket.send_text(text)

    async def stderr_output(self, chunk: bytes) -> None:
        """Number one CLI stderr chunk into the buffer; not retained, because the console renders
        chunks into lines and cannot identify a replayed chunk by position."""
        async with self._lock:
            text, _ = self._stamp(SetupOutput(data=chunk), retain=False)
            await self._outbound.send(text)

    async def initialized(self, stdin: StdinWriter) -> None:
        """Write the harness handshake, if this harness has one, echoing it into the record."""
        payload = self._driver.initialize()
        if payload is None:
            return
        await self._inject(payload)
        await stdin.write_object(payload)

    async def observed(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """One CLI stdout frame: number it, send it, journal its meaning; returns the native reply
        to write back for a CLI-initiated control request, already echoed into the record."""
        async with self._lock:
            wire, seq = self._stamp(HarnessFrame(frame=payload), retain=True)
            await self._outbound.send(wire)
            for text in self._journalled(self._driver.observe(seq, payload)):
                await self._outbound.send(text)
            reply = self._driver.answer_control_request(payload)
            if reply is not None:
                echo, _ = self._stamp(HarnessFrame(frame=reply, injected=True), retain=True)
                await self._outbound.send(echo)
            return reply

    async def admit(self, dispatch: PromptDispatch) -> dict[str, Any] | None:
        """One dispatched prompt: the native frame to write to the CLI, or None for a duplicate.

        The admission is journalled at the injection fence with the journal's own frontier, and
        the injected frame is echoed under the seq the provenance names — all before the caller
        writes the CLI, so the record can never show output of a prompt it has no injection for.
        """
        if dispatch.prompt_id in self._taken_prompts:
            return None
        self._taken_prompts.add(dispatch.prompt_id)
        payload = self._driver.compose_prompt(dispatch.text)
        async with self._lock:
            echo, seq = self._stamp(HarnessFrame(frame=payload, injected=True), retain=True)
            await self._outbound.send(echo)
            projected = self._driver.admit(
                dispatch.prompt_id, after_batch_seq=self._journal.admission_frontier, frame_seq=seq
            )
            for text in self._journalled(projected):
                await self._outbound.send(text)
        return payload

    async def interrupt(self) -> dict[str, Any] | None:
        """The operator's stop: the native interrupt to write, or None for a harness without one.

        The next turn end this pump journals is rewritten `aborted` — the side that asked records
        the abort, and under this generation the runner is the side that asks the harness.
        """
        payload = self._driver.compose_interrupt()
        if payload is None:
            return None
        await self._inject(payload)
        self._abort_pending = True
        return payload

    async def flushed(self) -> None:
        """The diagnostics-only tail a CLI may end on, released through the journal's own gate."""
        async with self._lock:
            for batch in self._journal.flush():
                await self._outbound.send(_journal_text(batch))

    async def acked(self, ack: BatchAck) -> None:
        """The console's cumulative ACK: drop covered retention, send whatever it released."""
        async with self._lock:
            for batch in self._journal.acked(ack.acked_batch_seq):
                await self._outbound.send(_journal_text(batch))

    async def _inject(self, payload: dict[str, Any]) -> int:
        async with self._lock:
            echo, seq = self._stamp(HarnessFrame(frame=payload, injected=True), retain=True)
            await self._outbound.send(echo)
            return seq

    def _stamp(self, frame: HarnessFrame | SetupOutput, *, retain: bool) -> tuple[str, int]:
        seq, self._next_seq = self._next_seq, self._next_seq + 1
        text = frame.model_copy(update={"seq": seq}).model_dump_json()
        if retain:
            self._retained.append((seq, text))
        return text, seq

    def _journalled(self, projected: Projected) -> list[str]:
        batches = self._journal.record(self._abort_rewritten(projected.operations), projected.unprojected)
        return [_journal_text(batch) for batch in batches]

    def _abort_rewritten(self, operations: tuple[Operation, ...]) -> tuple[Operation, ...]:
        if not self._abort_pending:
            return operations
        rewritten: list[Operation] = []
        for operation in operations:
            match operation:
                case TurnOpened():
                    # A fresh exchange: whatever the interrupt was for has already ended.
                    self._abort_pending = False
                    rewritten.append(operation)
                case TurnEnded() if self._abort_pending:
                    self._abort_pending = False
                    rewritten.append(
                        TurnEnded(turn_id=operation.turn_id, end=TurnAborted(), provenance=operation.provenance)
                    )
                case _:
                    rewritten.append(operation)
        return tuple(rewritten)
