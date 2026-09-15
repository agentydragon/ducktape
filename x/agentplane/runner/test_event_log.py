"""A torn final append cannot become a replayed Event or damage its committed prefix."""

from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.protocol import event_pb2
from x.agentplane.runner.event_log import EventLog

# gazelle:include_dep @pypi//protobuf


def test_every_partial_final_record_recovers_the_exact_prefix(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, "test-event-source")
    first = log.append(event_pb2.HarnessStarted(pid=123))
    prefix = path.read_bytes()
    log.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"text":"雪"}'))
    last_record = path.read_bytes()[len(prefix) :]
    log.close()

    for cut in range(len(last_record)):
        path.write_bytes(prefix + last_record[:cut])
        recovered = EventLog(path, "test-event-source")
        assert recovered.entries == [first]
        assert path.read_bytes() == prefix
        next_entry = recovered.append(event_pb2.TextDelta(item_id="test-item", text="next"))
        assert next_entry.cursor == first.cursor + 1
        recovered.close()
        reopened = EventLog(path, "test-event-source")
        assert reopened.entries == [first, next_entry]
        reopened.close()


@pytest.mark.parametrize("corrupt", [b'{"cursor":\n', b"{}\n", b"\n"])
@pytest.mark.parametrize("has_later_record", [False, True], ids=["last-record", "interior-record"])
def test_complete_corrupt_records_are_rejected_without_truncation(
    tmp_path: Path, corrupt: bytes, has_later_record: bool
) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog(path, "test-event-source")
    log.append(event_pb2.HarnessStarted(pid=123))
    prefix = path.read_bytes()
    log.close()
    contents = prefix + corrupt + (prefix if has_later_record else b"")
    path.write_bytes(contents)

    with pytest.raises(ValueError, match="corrupt session log"):
        EventLog(path, "test-event-source")
    assert path.read_bytes() == contents


if __name__ == "__main__":
    pytest_bazel.main()
