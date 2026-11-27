from dataclasses import replace

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci.bes import Invocation, TestSummary
from devinfra.ci.retry_snapshot_restore import failed_job, restore_failure

INVOCATION = Invocation(outputs=[], command="test", finished=True, exit_code_name="REMOTE_ERROR")
RESTORE = {
    "stage": "COMPLETED",
    "actionMnemonic": "TestRunner",
    "exitCode": -2,
    "status": {
        "code": 14,
        "message": "start machine: failed to load a snapshot for VM: [PUT /snapshot/load][400] loadSnapshotBadRequest  "
        "&{FaultMessage:Load snapshot error: Failed to restore from snapshot: Failed to build microVM from snapshot: "
        "Could not set TSC scaling within the snapshot...",
    },
}
PASSED = {"stage": "COMPLETED", "status": {}}


def test_restore_after_bazels_own_remote_retry() -> None:
    assert restore_failure(INVOCATION, [PASSED, RESTORE, RESTORE])


@pytest.mark.parametrize(
    "execution",
    [
        {"stage": "EXECUTING"},
        {"stage": "COMPLETED", "status": {}, "actionMnemonic": "TestRunner", "exitCode": 1},
        {**RESTORE, "status": {"code": 1, "message": "cancelled"}},
        {**RESTORE, "status": {"code": 14, "message": "worker lost connection"}},
        {**RESTORE, "actionMnemonic": "GoCompilePkg"},
    ],
)
def test_unrelated_failure_vetoes_retry(execution: dict) -> None:
    assert not restore_failure(INVOCATION, [RESTORE, execution])


def test_requires_failed_remote_test_with_no_test_failures() -> None:
    assert not restore_failure(INVOCATION, [PASSED])
    assert not restore_failure(replace(INVOCATION, finished=False), [RESTORE])
    assert not restore_failure(replace(INVOCATION, exit_code_name="TESTS_FAILED"), [RESTORE])
    failed = TestSummary("//:other", "config", "FAILED", 1, 0)
    assert not restore_failure(replace(INVOCATION, test_summaries=[failed]), [RESTORE])


def test_only_one_failed_remote_step_and_complete_jobs_page() -> None:
    job = {
        "id": 42,
        "name": "bazel-ci / Test & Build",
        "conclusion": "failure",
        "steps": [{"name": "Test + Build + lint", "conclusion": "failure"}],
    }
    assert failed_job({"jobs": [job, {"conclusion": "skipped"}], "total_count": 2}) == 42
    assert failed_job({"jobs": [job], "total_count": 2}) is None
    assert failed_job({"jobs": [job, {"conclusion": "cancelled"}], "total_count": 2}) is None
    assert (
        failed_job({"jobs": [{**job, "steps": [{"name": "checkout", "conclusion": "failure"}]}], "total_count": 1})
        is None
    )


if __name__ == "__main__":
    pytest_bazel.main()
