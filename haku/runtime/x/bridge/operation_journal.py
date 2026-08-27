"""The runner's journal of neutral-operation batches: immediate flush, ACK-gated coalescing.

The batching half of the #4667 boundary, implementing the operator's streaming-latency ruling
exactly: **operations flush the moment they are recorded, and coalesce into one batch only while
an ACK is in flight.** No accumulation timers and no delay knob exist — on a healthy link partial
text tracks the native stream at one RTT plus commit, and a slow consumer grows batches under
backpressure instead of stalling the stream.

Sans-IO: the journal is a state machine over <neutral_operations.py> shapes, and every mutating
call returns the batches the caller must now put on the wire, in order. The transport that owns
the socket (the runner's, at the generation cut) drives it:

- `record(...)` after each projector yield; send what comes back.
- `acked(n)` on each `BatchAck`; send what comes back — the coalesced batch the ACK released.
- `resume(n)` on each `ConsoleResume`, reconnects included; send what comes back — the retained
  replay above the Console's durable cursor.
- `flush()` when the CLI ends, for a diagnostics-only tail no operation would ever carry out.

**One rule decides every cut**: a new batch is numbered only while nothing already sent awaits an
ACK. Batches are numbered densely from 1 at the moment they are cut — never re-numbered — so a
replayed batch keeps the seq it first went out under and two Consoles agree on what one integer
names. Everything sent is retained in memory until an ACK (cumulative) or a resume cursor covers
it; retention is unbounded by design, because the contract is that an unacknowledged batch is
replayable, and the cut rule bounds growth to one batch per round trip. A runner/session stays
terminal on runner loss, so nothing here persists.

Diagnostics ride; they never drive. Unprojected counts wait for the next operation-bearing batch
rather than spending the one in-flight slot on telemetry — an operation produced a moment later
would otherwise wait a full round trip behind it. The one exception is `flush`, for the tail.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from haku.runtime.x.bridge.neutral_operations import (
    NEUTRAL_PROTOCOL_VERSION,
    BatchDiagnostics,
    Operation,
    OperationBatch,
)

_NOTHING: Mapping[str, int] = MappingProxyType({})


class OperationJournal:
    """One session's monotonic journal of `OperationBatch`es, and the retention that replays it."""

    def __init__(self) -> None:
        self._next_seq = 1
        self._pending_operations: list[Operation] = []
        self._pending_unprojected: Counter[str] = Counter()
        # Cut and handed to the caller, not yet covered by an ACK or resume cursor: exactly what a
        # reconnecting Console may be missing, in seq order.
        self._retained: deque[OperationBatch] = deque()

    @property
    def admission_frontier(self) -> int | None:
        """The last already-numbered batch, as `PromptAdmitted.after_batch_seq` states it.

        Read at the injection fence and passed to the projector's `admit`: operations pending
        beside the admission share its eventual batch, where their order is their position, so the
        frontier only ever names a batch that already has a number.
        """
        return self._next_seq - 1 if self._next_seq > 1 else None

    def record(
        self, operations: Iterable[Operation] = (), unprojected: Mapping[str, int] = _NOTHING
    ) -> tuple[OperationBatch, ...]:
        """Take one projector yield into the journal; returns the batch to send now, if any."""
        for key, count in unprojected.items():
            if count < 1:
                raise ValueError(f"an unprojected count reports occurrences: {key=} {count=}")
        self._pending_operations.extend(operations)
        self._pending_unprojected.update(unprojected)
        return self._cut_if_clear()

    def acked(self, acked_batch_seq: int) -> tuple[OperationBatch, ...]:
        """The Console durably committed everything up to *acked_batch_seq*.

        Drops the covered retention and returns the batch the ACK released — whatever coalesced
        while it was in flight. Cumulative, so an ACK repeating or preceding one already applied
        drops nothing and is not an error.
        """
        self._drop_covered(acked_batch_seq)
        return self._cut_if_clear()

    def resume(self, acked_batch_seq: int | None) -> tuple[OperationBatch, ...]:
        """A (re)connected Console reported its durable cursor; returns everything to send now.

        The replay is every retained batch above the cursor, verbatim under its original seq — a
        batch seen twice is the same batch, and the Console's commit is idempotent by seq. The
        cursor also stands in for ACKs the old socket lost: what it covers is dropped exactly as
        an ACK would have. With nothing left to replay, pending operations cut immediately — a
        fresh connection has no ACK in flight to coalesce behind.
        """
        if acked_batch_seq is not None:
            self._drop_covered(acked_batch_seq)
        if self._retained:
            return tuple(self._retained)
        return self._cut_if_clear()

    def flush(self) -> tuple[OperationBatch, ...]:
        """Cut whatever is pending even with no operation to carry it — the diagnostics-only tail
        a session may end on. Still ACK-gated: with a batch in flight there is nothing to do yet,
        and the ACK or the next resume releases the tail through `acked`/`resume` as usual."""
        if self._retained or not (self._pending_operations or self._pending_unprojected):
            return ()
        return (self._cut(),)

    def _drop_covered(self, acked_batch_seq: int) -> None:
        if acked_batch_seq >= self._next_seq:
            raise ValueError(
                f"the Console acknowledged a batch this journal never cut: {acked_batch_seq=} {self._next_seq - 1=}"
            )
        while self._retained and self._retained[0].runner_batch_seq <= acked_batch_seq:
            self._retained.popleft()

    def _cut_if_clear(self) -> tuple[OperationBatch, ...]:
        """The immediate-flush rule: cut the pending operations now unless an ACK is in flight.

        Diagnostics alone do not cut — they ride the next operation-bearing batch (or `flush`) —
        so a burst of unmapped frames cannot hold the in-flight slot against the conversation.
        """
        if self._retained or not self._pending_operations:
            return ()
        return (self._cut(),)

    def _cut(self) -> OperationBatch:
        batch = OperationBatch(
            neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION,
            runner_batch_seq=self._next_seq,
            operations=tuple(self._pending_operations),
            diagnostics=BatchDiagnostics(unprojected=dict(self._pending_unprojected)),
        )
        self._next_seq += 1
        self._pending_operations.clear()
        self._pending_unprojected.clear()
        self._retained.append(batch)
        return batch
