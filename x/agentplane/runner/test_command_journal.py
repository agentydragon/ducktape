"""Recovery preserves command identity and outcomes while discarding incomplete appends."""

from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.protocol import command_pb2, event_pb2
from x.agentplane.runner.command_journal import CommandConflictError, CommandJournal


def test_partial_admission_and_transitions_recover_previous_durable_facts(tmp_path: Path) -> None:
    path = tmp_path / "commands.jsonl"
    command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
    journal = CommandJournal(path)
    snapshots = [list(journal.entries)]
    journal.admit(command)
    snapshots.append(list(journal.entries))
    journal.dispatch_planned(command.command_id, native_correlation={"request_id": "test-native-request"})
    snapshots.append(list(journal.entries))
    journal.native_effect_observed(command.command_id)
    snapshots.append(list(journal.entries))
    journal.terminal(
        command.command_id,
        outcome=event_pb2.HarnessUserMessageConfirmed(
            harness_message_id="test-message", text="hello", origin_command_ids=[command.command_id]
        ),
    )
    snapshots.append(list(journal.entries))
    journal.close()
    records = path.read_bytes().splitlines(keepends=True)

    for index, record in enumerate(records):
        prefix = b"".join(records[:index])
        for cut in range(len(record)):
            path.write_bytes(prefix + record[:cut])
            recovered = CommandJournal(path)
            assert recovered.entries == snapshots[index]
            assert path.read_bytes() == prefix
            recovered.close()

    # A complete terminal record remains authoritative after another admission is torn off.
    path.write_bytes(b"".join(records) + b'{"record":"admitted","command":')
    recovered = CommandJournal(path)
    assert recovered.entries == snapshots[-1]
    assert not recovered.admit(command)
    with pytest.raises(CommandConflictError):
        recovered.admit(
            command_pb2.Command(command_id=command.command_id, submit_input=command_pb2.SubmitInput(text="changed"))
        )
    recovered.close()
    assert path.read_bytes() == b"".join(records)


@pytest.mark.parametrize("corrupt", [b'{"record":\n', b"{}\n", b"\n"])
@pytest.mark.parametrize("has_later_record", [False, True], ids=["last-record", "interior-record"])
def test_complete_corrupt_records_are_rejected_without_truncation(
    tmp_path: Path, corrupt: bytes, has_later_record: bool
) -> None:
    path = tmp_path / "commands.jsonl"
    journal = CommandJournal(path)
    journal.admit(command_pb2.Command(command_id="test-stop", stop_runner_session=command_pb2.StopRunnerSession()))
    prefix = path.read_bytes()
    journal.close()
    contents = prefix + corrupt + (prefix if has_later_record else b"")
    path.write_bytes(contents)

    with pytest.raises(ValueError, match="corrupt command journal"):
        CommandJournal(path)
    assert path.read_bytes() == contents


if __name__ == "__main__":
    pytest_bazel.main()
