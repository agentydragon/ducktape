"""The session API a harness run-loop pumps through: one sequence, retention, journal, admission.

Harness-invariant once a native stream has been reduced to neutral operations. Each harness owns
its own run-loop (<backend.py> `Harness.run`); it hands `SessionApi` each CLI stdout frame together
with the projection of it, the native input it composed for a prompt or an interrupt, and consumes
the console's commands back. `SessionApi` numbers everything this end sends on one dense sequence,
retains a replay window, and folds the stream into the acknowledged `OperationJournal`
(<operation_journal.py>). The console transport that carries all this is <communicator.py>.

**One sequence numbers everything this end sends** — stdout frames, setup narration, injected
input — minted where the event happens rather than where the socket is, so the seq the projector
stamps into provenance is the seq the recorded frame carries, and both survive the socket that
happens to be up.

**The harness projects; `SessionApi` numbers, journals and retains.** The seq a projection's
provenance names is assigned here, under the one lock, so the harness passes its projection as a
callback taken with that seq rather than computing it against a number it does not own. Everything
native the harness composes — the prompt frame, the interrupt, a control reply — is handed in as
the frame to echo; the injection fence, retention and abort rewrite stay here. The seq/retention/
journal/abort/admission machinery is not the harness's to reimplement.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from haku.runner.neutral_operations import (
    BatchAck,
    ConsoleResume,
    Operation,
    OperationBatch,
    TurnAborted,
    TurnEnded,
    TurnOpened,
)
from haku.runner.operation_journal import OperationJournal
from haku.runner.projection import Projected
from haku.runner.protocol import HarnessFrame, Interrupt, PromptDispatch, RunnerJournal, SetupOutput, TextWebSocket

# How many already-sent frames are kept to hand a console that adopts this session: a window over
# what a dying console may not have recorded, not a second copy of the rollout. Sized for a turn's
# assistant messages and tool results, which is what a roll mid-turn can strand. Journal batches
# are retained separately and unbounded, by the ACK contract (<operation_journal.py>).
REPLAY_WINDOW = 500

# What a harness's run-loop receives from the console: a prompt to inject at its native fence, or a
# stop for the running exchange. The neutral-generation console composes no native input, so those
# are the only two the serve loop hands on.
type ConsoleCommand = PromptDispatch | Interrupt

# The projection a harness hands `observe`: its projector's `observe`, called with the frame seq
# `SessionApi` assigned and the frame itself.
type Project = Callable[[int, dict[str, Any]], Projected]
# The projection a harness hands `admit`, taken with the journal's admission frontier and the
# injected frame's seq.
type ProjectAdmission = Callable[..., Projected]
# The native reply a harness composes for one CLI stdout frame — a control-request refusal — or
# None for a frame that asks nothing.
type Answer = Callable[[dict[str, Any]], dict[str, Any] | None]


def _journal_text(batch: OperationBatch) -> str:
    return RunnerJournal(message=batch).model_dump_json()


class SessionApi:
    """One session's numbering, retention, journal and admission, across every socket.

    **The number is this end's to mint**, because this end survives: the console is replaced on
    every roll while the harness holds the CLI across as many sockets as that takes. Everything
    stamped goes out through one buffer under one lock, so the wire order is the stamp order; a
    reconnect replays retained frames above the console's frame cursor and retained journal batches
    above its batch cursor, and the console deduplicates both — frames by `runner_seq`, batches by
    idempotent commit.
    """

    def __init__(self, outbound: MemoryObjectSendStream[str], *, window: int = REPLAY_WINDOW):
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
        # The console's commands to this session's harness. Unbounded: the serve loop must never
        # block handing one on, and prompts a busy harness has not consumed yet are few and small.
        self._commands_send, self._commands_receive = anyio.create_memory_object_stream[ConsoleCommand](math.inf)

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

    async def deliver(self, command: ConsoleCommand) -> None:
        """Hand one console command to the harness's run-loop. Never blocks (unbounded buffer)."""
        await self._commands_send.send(command)

    async def commands(self) -> AsyncIterator[ConsoleCommand]:
        """The console's prompts and interrupts for this session, in the order they arrived."""
        async for command in self._commands_receive:
            yield command

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

    async def observe(self, payload: dict[str, Any], project: Project, answer: Answer) -> dict[str, Any] | None:
        """One CLI stdout frame: number it, send it, journal its meaning; returns the native reply
        to write back for a CLI-initiated control request, already echoed into the record.

        `project` is the harness's projection of this frame, taken with the seq assigned here so its
        provenance names the recorded frame. `answer` composes the native reply the frame calls for,
        echoed under the same lock so the record can never show a reply out of order with the frame.
        """
        async with self._lock:
            wire, seq = self._stamp(HarnessFrame(frame=payload), retain=True)
            await self._outbound.send(wire)
            for text in self._journalled(project(seq, payload)):
                await self._outbound.send(text)
            reply = answer(payload)
            if reply is not None:
                echo, _ = self._stamp(HarnessFrame(frame=reply, injected=True), retain=True)
                await self._outbound.send(echo)
            return reply

    async def admit(
        self, prompt_id: UUID, compose: Callable[[], dict[str, Any]], project: ProjectAdmission
    ) -> dict[str, Any] | None:
        """One dispatched prompt: the native frame to write to the CLI, or None for a duplicate.

        The admission is journalled at the injection fence with the journal's own frontier, and the
        injected frame is echoed under the seq the provenance names — all before the caller writes
        the CLI, so the record can never show output of a prompt it has no injection for. `compose`
        is the harness's native input frame; `project` its `prompt.admitted` (and the turn it opens
        while idle), taken with the frontier and the injected frame's seq.
        """
        if prompt_id in self._taken_prompts:
            return None
        self._taken_prompts.add(prompt_id)
        payload = compose()
        async with self._lock:
            echo, seq = self._stamp(HarnessFrame(frame=payload, injected=True), retain=True)
            await self._outbound.send(echo)
            projected = project(after_batch_seq=self._journal.admission_frontier, frame_seq=seq)
            for text in self._journalled(projected):
                await self._outbound.send(text)
        return payload

    async def interrupt(self, compose: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        """The operator's stop: the native interrupt to write, or None when the harness has none.

        The next turn end this session journals is rewritten `aborted` — the side that asked records
        the abort, and under this generation the runner is the side that asks the harness.
        """
        payload = compose()
        if payload is None:
            return None
        await self.inject(payload)
        self._abort_pending = True
        return payload

    async def inject(self, payload: dict[str, Any]) -> int:
        """Number, send and retain one frame of native input the harness wrote itself, echoing it
        into the record `injected`. Returns the seq, which a harness's handshake correlates on."""
        async with self._lock:
            echo, seq = self._stamp(HarnessFrame(frame=payload, injected=True), retain=True)
            await self._outbound.send(echo)
            return seq

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
