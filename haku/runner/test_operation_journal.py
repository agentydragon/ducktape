"""The journal's batching contract: immediate flush, ACK-gated coalescing, replay by cursor.

The schedules below are the operator's amendment on #4667 played out: a healthy link gets one
batch per yield, a withheld ACK grows one coalesced batch instead of a queue, and a reconnect
replays exactly what the Console's durable cursor says it is missing. The golden stage-1 fixtures
tie the two stages together: the operation stream those files pin re-journals losslessly, and the
`ConsoleResume`/`BatchAck` lines are read with the same adapter the transport will use.
"""

from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel

from haku.runner.neutral_operations import (
    CONSOLE_TO_RUNNER,
    NEUTRAL_PROTOCOL_VERSION,
    RUNNER_TO_CONSOLE,
    BatchAck,
    ConsoleResume,
    FrameRange,
    ItemSegment,
    Operation,
    OperationBatch,
    TurnOpened,
    WakeCause,
)
from haku.runner.operation_journal import OperationJournal
from util.bazel.runfiles import get_required_path

_TESTDATA = "haku/runner/testdata"


def _golden_lines(name: str) -> list[str]:
    source = Path(f"{_TESTDATA}/{name}")
    path = source if source.exists() else get_required_path(f"ducktape/{_TESTDATA}/{name}")
    return path.read_text().splitlines()


def _segment(text: str) -> ItemSegment:
    return ItemSegment(
        item_id=UUID("33333333-3333-4333-8333-333333333301"),
        text=text,
        provenance=FrameRange(first_frame_seq=1, last_frame_seq=1),
    )


def test_operations_flush_the_moment_nothing_is_in_flight():
    journal = OperationJournal()

    first = journal.record([_segment("a")])
    assert [batch.runner_batch_seq for batch in first] == [1]
    assert first[0].operations == (_segment("a"),)

    assert journal.acked(1) == ()
    second = journal.record([_segment("b")])
    assert [batch.runner_batch_seq for batch in second] == [2]


def test_a_withheld_ack_coalesces_into_one_batch_and_its_ack_releases_it():
    """No timers and no knob: what accumulates while the ACK is in flight is exactly one batch,
    cut by the ACK's arrival and never before."""
    journal = OperationJournal()
    journal.record([_segment("a")])

    assert journal.record([_segment("b")]) == ()
    assert journal.record([_segment("c")]) == ()

    released = journal.acked(1)
    assert [batch.runner_batch_seq for batch in released] == [2]
    assert released[0].operations == (_segment("b"), _segment("c"))
    assert journal.acked(2) == ()


def test_an_ack_is_cumulative_and_repeatable():
    journal = OperationJournal()
    journal.record([_segment("a")])
    journal.acked(1)
    journal.record([_segment("b")])

    # A repeated or preceding ACK drops nothing and is not an error.
    assert journal.acked(1) == ()
    assert journal.acked(2) == ()
    assert journal.acked(2) == ()


def test_a_reconnect_replays_retained_batches_under_their_original_numbers():
    journal = OperationJournal()
    sent = journal.record([_segment("a")])
    assert journal.record([_segment("b")]) == ()  # pending behind the un-ACKed batch

    replayed = journal.resume(None)
    assert replayed == sent
    assert [batch.runner_batch_seq for batch in replayed] == [1]

    # The replay is outstanding again; the pending operations wait for its ACK as they did before.
    released = journal.acked(1)
    assert [batch.runner_batch_seq for batch in released] == [2]
    assert released[0].operations == (_segment("b"),)


def test_a_resume_cursor_stands_in_for_the_acks_the_old_socket_lost():
    journal = OperationJournal()
    journal.record([_segment("a")])
    assert journal.record([_segment("b")]) == ()

    # The Console committed batch 1 but its ACK died with the connection: the cursor covers it,
    # and with nothing left to replay the pending operations cut immediately — a fresh connection
    # has no ACK in flight to coalesce behind.
    resumed = journal.resume(1)
    assert [batch.runner_batch_seq for batch in resumed] == [2]
    assert resumed[0].operations == (_segment("b"),)


def test_a_resume_with_nothing_retained_and_nothing_pending_sends_nothing():
    assert OperationJournal().resume(None) == ()


def test_an_ack_for_a_batch_never_cut_rejects():
    journal = OperationJournal()
    with pytest.raises(ValueError, match="never cut"):
        journal.acked(1)
    journal.record([_segment("a")])
    with pytest.raises(ValueError, match="never cut"):
        journal.resume(5)


def test_diagnostics_ride_the_next_batch_and_never_drive_one():
    """Unprojected counts are telemetry: they wait for an operation-bearing batch rather than
    spending the one in-flight slot, and `flush` exists for the tail a session ends on."""
    journal = OperationJournal()

    assert journal.record(unprojected={"tool_progress": 2}) == ()
    carried = journal.record([_segment("a")], unprojected={"tool_progress": 1})
    assert carried[0].diagnostics.unprojected == {"tool_progress": 3}

    assert journal.record(unprojected={"telepathy_event": 1}) == ()
    assert journal.flush() == ()  # the ACK for batch 1 is still in flight
    assert journal.acked(1) == ()  # nothing operation-bearing pending, so the ACK cuts nothing
    tail = journal.flush()
    assert [batch.runner_batch_seq for batch in tail] == [2]
    assert tail[0].operations == ()
    assert tail[0].diagnostics.unprojected == {"telepathy_event": 1}
    assert journal.flush() == ()  # the tail batch is outstanding now, and nothing else is pending


def test_a_zero_unprojected_count_rejects():
    with pytest.raises(ValueError, match="occurrences"):
        OperationJournal().record(unprojected={"tool_progress": 0})


def test_the_admission_frontier_names_the_last_numbered_batch():
    journal = OperationJournal()
    assert journal.admission_frontier is None

    journal.record([_segment("a")])
    assert journal.admission_frontier == 1
    journal.record([_segment("b")])  # pending — not numbered, so not the frontier
    assert journal.admission_frontier == 1

    journal.acked(1)
    assert journal.admission_frontier == 2


def test_every_cut_batch_round_trips_the_wire():
    journal = OperationJournal()
    journal.record([TurnOpened(turn_id=UUID(int=7), cause=WakeCause(), provenance=None)])
    journal.record([_segment("a")], unprojected={"tool_progress": 1})
    batches = [*journal.resume(None), *journal.acked(1)]

    for batch in batches:
        assert RUNNER_TO_CONSOLE.validate_json(batch.model_dump_json()) == batch


def test_the_golden_operation_stream_re_journals_losslessly():
    """The stage-1 contract fixtures, replayed through this journal: the golden operations go in
    one yield at a time under a schedule of withheld ACKs and one reconnect, and come out in the
    same order under dense numbering with the golden diagnostics preserved."""
    golden: list[Operation] = []
    diagnostics_in: dict[str, int] = {}
    for line in _golden_lines("neutral_v1_runner_to_console.jsonl"):
        message = RUNNER_TO_CONSOLE.validate_json(line)
        if isinstance(message, OperationBatch):
            golden.extend(message.operations)
            for key, occurrences in message.diagnostics.unprojected.items():
                diagnostics_in[key] = diagnostics_in.get(key, 0) + occurrences

    journal = OperationJournal()
    produced: list[OperationBatch] = []
    for position, operation in enumerate(golden):
        produced.extend(journal.record([operation]))
        if position == 3:
            journal.record(unprojected=diagnostics_in)
        if position % 5 == 4:
            produced.extend(journal.acked(produced[-1].runner_batch_seq))
        if position == 10:
            # A reconnect mid-stream: the Console's cursor covers everything numbered so far.
            produced.extend(journal.resume(produced[-1].runner_batch_seq))
    produced.extend(journal.acked(produced[-1].runner_batch_seq))
    produced.extend(journal.flush())

    replayed = [operation for batch in produced for operation in batch.operations]
    assert replayed == golden
    assert [batch.runner_batch_seq for batch in produced] == list(range(1, len(produced) + 1))
    carried: dict[str, int] = {}
    for batch in produced:
        for key, occurrences in batch.diagnostics.unprojected.items():
            carried[key] = carried.get(key, 0) + occurrences
    assert carried == diagnostics_in
    for batch in produced:
        assert batch.neutral_protocol_version == NEUTRAL_PROTOCOL_VERSION
        assert RUNNER_TO_CONSOLE.validate_json(batch.model_dump_json()) == batch


def test_the_golden_console_lines_read_with_the_transport_adapter():
    """The other direction of the stage-1 contract, as the runner's transport will read it: the
    resume line carries the cursor `resume` takes, and every ACK line carries the seq `acked`
    takes."""
    messages = [CONSOLE_TO_RUNNER.validate_json(line) for line in _golden_lines("neutral_v1_console_to_runner.jsonl")]

    resume = messages[0]
    assert isinstance(resume, ConsoleResume)
    journal = OperationJournal()
    assert journal.resume(resume.acked_batch_seq) == ()

    acks = [message for message in messages[1:] if isinstance(message, BatchAck)]
    assert [ack.acked_batch_seq for ack in acks] == [1, 2, 4]
    for ack in acks:
        # Number batches up to the golden seq, releasing each so the next one cuts; the golden
        # ACK then applies cleanly — cumulative, covered, and cutting nothing further.
        while (journal.admission_frontier or 0) < ack.acked_batch_seq:
            journal.record([_segment("x")])
            journal.acked(journal.admission_frontier or 0)
        assert journal.acked(ack.acked_batch_seq) == ()


if __name__ == "__main__":
    pytest_bazel.main()
