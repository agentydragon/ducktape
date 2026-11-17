"""Persistence and no-replay checks for provider import reservations."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel
from filelock import Timeout

from platform_sessions.provider.v1 import session_provider_pb2 as pb
from platform_sessions.provider.v1.import_journal import ImportJournal, ImportState, validate_operation_id


def test_reservation_is_unique_and_incomplete_operation_survives_restart(tmp_path: Path) -> None:
    operation_id = str(uuid4())
    journal = ImportJournal(tmp_path)
    with journal._transaction() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "delete"
        assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 3
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(pool.map(journal.reserve, [operation_id] * 16))
    assert sum(created for created, _ in reservations) == 1
    assert all(record.state is ImportState.PENDING for _, record in reservations)
    journal.close()

    restarted = ImportJournal(tmp_path)
    created, record = restarted.reserve(operation_id)
    assert not created
    assert record.state is ImportState.UNKNOWN
    with pytest.raises(ValueError, match="not pending"):
        restarted.complete(operation_id, pb.ImportSessionResponse(outcome=pb.IMPORT_OUTCOME_INSTALLED))
    restarted.close()


def test_completed_result_is_sanitized_and_recovered_without_candidate(tmp_path: Path) -> None:
    operation_id = str(uuid4())
    journal = ImportJournal(tmp_path)
    assert journal.reserve(operation_id)[0]
    result = pb.ImportSessionResponse(
        instance=pb.InstanceRef(provider_id="candidate-secret", instance_id="candidate-secret"),
        outcome=pb.IMPORT_OUTCOME_UNKNOWN,
        outcome_unknown=pb.OperationOutcomeUnknown(credential_rotation_may_have_occurred=True),
    )
    journal.complete(operation_id, result)
    journal.close()
    assert b"candidate-secret" not in (tmp_path / "import-operations.sqlite3").read_bytes()

    restarted = ImportJournal(tmp_path)
    created, record = restarted.reserve(operation_id)
    assert not created
    assert record.state is ImportState.COMPLETED
    assert record.result is not None
    assert record.result.outcome == pb.IMPORT_OUTCOME_UNKNOWN
    assert record.result.outcome_unknown.credential_rotation_may_have_occurred
    assert not record.result.HasField("instance")
    restarted.close()


def test_exclusive_owner_and_invalid_operation_ids(tmp_path: Path) -> None:
    journal = ImportJournal(tmp_path)
    with pytest.raises(Timeout):
        ImportJournal(tmp_path)
    for invalid in ("", "../escape", "00000000-0000-0000-0000-000000000000", str(uuid4()).upper()):
        with pytest.raises(ValueError, match="invalid import operation ID"):
            validate_operation_id(invalid)
    journal.close()
    successor = ImportJournal(tmp_path)
    successor.close()


def test_invalid_result_cannot_complete_reservation(tmp_path: Path) -> None:
    journal = ImportJournal(tmp_path)
    operation_id = str(uuid4())
    journal.reserve(operation_id)
    with pytest.raises(ValueError, match="Candidate operation has no outcome"):
        journal.complete(operation_id, pb.ImportSessionResponse())
    record = journal.get(operation_id)
    assert record is not None
    assert record.state is ImportState.PENDING
    journal.close()


if __name__ == "__main__":
    pytest_bazel.main()
