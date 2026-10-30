import concurrent.futures
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from devinfra.buildbuddy.ci import invocation_census
from devinfra.buildbuddy.ci.invocation_census import (
    QueryFailure,
    StatusResponse,
    collect_statuses,
    parse_status_response,
    query_live,
    query_offline,
    select_invocations,
    state_for,
)
from devinfra.buildbuddy.ci.invocation_ids import invocation_id


def snapshot() -> dict[str, Any]:
    return {
        "collected_at": "2026-10-30T12:00:00Z",
        "runs": [
            {
                "id": 100,
                "run_attempt": 2,
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "created_at": "2026-10-30T10:00:00Z",
                "conclusion": "success",
                "head_sha": "actions-synthetic-or-event-head",
            },
            {
                "id": 200,
                "run_attempt": 1,
                "event": "pull_request",
                "path": ".github/workflows/ci.yml",
                "created_at": "2026-10-30T11:00:00Z",
                "conclusion": "failure",
                "head_sha": "actions-pr-merge-sha",
            },
        ],
        "jobs": [
            {
                "id": 1001,
                "run_id": 100,
                "run_attempt": 1,
                "name": "bazel-ci / Test & Build",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "actions-synthetic-or-event-head",
                "runner_id": 123,
            },
            {
                "id": 1002,
                "run_id": 100,
                "run_attempt": 2,
                "name": "bazel-ci / Test & Build",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "actions-synthetic-or-event-head",
                "runner_id": 124,
            },
            {
                "id": 2001,
                "run_id": 200,
                "run_attempt": 1,
                "name": "bazel-ci / Test & Build",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "actions-pr-merge-sha",
                "runner_id": 125,
            },
            {"id": 2002, "run_id": 200, "run_attempt": 1, "name": "another job"},
        ],
    }


def test_selection_uses_shared_ids_and_each_job_attempt() -> None:
    requests = select_invocations(
        snapshot(), job_name="bazel-ci / Test & Build", roles=("test", "build"), events=("push",)
    )

    assert [(request.attempt, request.role) for request in requests] == [
        ("1", "build"),
        ("1", "test"),
        ("2", "build"),
        ("2", "test"),
    ]
    assert requests[0].invocation_id == str(invocation_id(run_id="100", attempt="1", role="build"))
    assert requests[2].invocation_id == str(invocation_id(run_id="100", attempt="2", role="build"))


def test_event_workflow_and_time_filters_use_run_created_at() -> None:
    requests = select_invocations(
        snapshot(),
        job_name="bazel-ci / Test & Build",
        roles=("test",),
        events=("pull_request",),
        workflow_path=".github/workflows/ci.yml",
        since=invocation_census.parse_timestamp("2026-10-30T11:00:00Z", option="--since"),
        until=invocation_census.parse_timestamp("2026-10-30T12:00:00Z", option="--until"),
    )

    assert len(requests) == 1
    assert requests[0].run["id"] == 200
    assert requests[0].role == "test"


def test_absent_build_and_removed_race_roles_are_missing_not_failures(tmp_path: Path) -> None:
    selected = select_invocations(
        snapshot(), job_name="bazel-ci / Test & Build", roles=("test", "build", "race"), events=("push",)
    )
    for request in selected:
        metadata = (
            (
                {
                    "invocationStatus": "COMPLETE_INVOCATION_STATUS",
                    "success": False,
                    "commitSha": "tested-source-sha",
                    "repoUrl": "https://github.com/agentydragon/ducktape",
                    "branchName": "reports-test-branch",
                    "command": "sensitive raw command should not be emitted",
                    "secret": "never emit me",
                },
            )
            if request.role == "test"
            else ()
        )
        (tmp_path / f"{request.invocation_id}.json").write_text(json.dumps({"metadata": list(metadata)}))

    records = collect_statuses(
        selected,
        query=lambda identifier: query_offline(identifier, response_dir=tmp_path),
        max_workers=2,
        query_mode="offline_replay",
        observed_at="2026-10-30T12:30:00Z",
    )
    by_role_attempt = {(record["role"], record["attempt"]): record for record in records}

    assert by_role_attempt["test", "1"]["state"] == "complete"
    assert by_role_attempt["test", "1"]["metadata"] == [
        {
            "invocationStatus": "COMPLETE_INVOCATION_STATUS",
            "success": False,
            "commitSha": "tested-source-sha",
            "repoUrl": "https://github.com/agentydragon/ducktape",
            "branchName": "reports-test-branch",
        }
    ]
    assert by_role_attempt["test", "1"]["buildbuddy_commit_shas"] == ["tested-source-sha"]
    assert by_role_attempt["test", "1"]["actions"]["actions_run_head_sha"] == "actions-synthetic-or-event-head"
    for attempt in ("1", "2"):
        assert by_role_attempt["build", attempt]["state"] == "missing"
        assert by_role_attempt["race", attempt]["state"] == "missing"
        assert by_role_attempt["build", attempt]["query_error"] is None
    assert all("flaky" not in record for record in records)
    assert "command" not in by_role_attempt["test", "1"]["metadata"][0]


def test_status_states_keep_unknown_disconnected_and_query_error_distinct() -> None:
    assert state_for(StatusResponse(())) == "missing"
    assert state_for(StatusResponse(({"invocationStatus": "COMPLETE_INVOCATION_STATUS"},))) == "complete"
    assert state_for(StatusResponse(({"invocationStatus": "DISCONNECTED_INVOCATION_STATUS"},))) == "disconnected"
    assert state_for(StatusResponse(({"invocationStatus": "FUTURE_STATUS"},))) == "unknown"
    assert (
        state_for(
            StatusResponse(
                (
                    {"invocationStatus": "COMPLETE_INVOCATION_STATUS"},
                    {"invocationStatus": "DISCONNECTED_INVOCATION_STATUS"},
                )
            )
        )
        == "mixed"
    )
    assert state_for(QueryFailure("timeout")) == "query_error"


def test_empty_proto_response_is_missing_but_unrecognized_response_is_invalid() -> None:
    assert parse_status_response({}) == StatusResponse(())
    with pytest.raises(ValueError, match="no invocation metadata"):
        parse_status_response({"error": "unrecognized response"})


def test_live_query_classifies_timeout_without_exposing_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: str, **kwargs: object) -> bytes:
        raise invocation_census.CLIError("private stderr") from subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(invocation_census, "bbapi", timeout)
    result = query_live("id", timeout_seconds=30)

    assert result == QueryFailure("timeout")


def test_live_query_classifies_command_and_invalid_json_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def command_failed(*args: str, **kwargs: object) -> bytes:
        raise invocation_census.CLIError("private stderr") from subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(invocation_census, "bbapi", command_failed)
    assert query_live("id", timeout_seconds=5) == QueryFailure("bbapi_failed")

    monkeypatch.setattr(invocation_census, "bbapi", lambda *args, **kwargs: b"not json")
    assert query_live("id", timeout_seconds=5) == QueryFailure("invalid_response")


def test_live_query_uses_pinned_bbapi_status_json_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, object] = {}

    def status(*args: str, **kwargs: object) -> bytes:
        called["args"] = args
        called["kwargs"] = kwargs
        return b'{"invocation": []}'

    monkeypatch.setattr(invocation_census, "bbapi", status)
    assert query_live("requested-id", timeout_seconds=9) == StatusResponse(())
    assert called == {"args": ("--json", "invocation", "status", "requested-id"), "kwargs": {"timeout": 9}}


def test_offline_query_distinguishes_missing_response_from_unknown_invocation(tmp_path: Path) -> None:
    missing = query_offline("missing-file", response_dir=tmp_path)
    (tmp_path / "unknown-invocation.json").write_text('{"invocation": []}')
    unknown = query_offline("unknown-invocation", response_dir=tmp_path)

    assert missing == QueryFailure("offline_response_missing")
    assert unknown == StatusResponse(())


def test_status_queries_respect_bounded_concurrency() -> None:
    requests = select_invocations(
        {
            "runs": [
                {
                    "id": run_id,
                    "run_attempt": 1,
                    "event": "push",
                    "created_at": "2026-10-30T10:00:00Z",
                    "head_sha": f"head-{run_id}",
                }
                for run_id in range(300, 306)
            ],
            "jobs": [
                {"id": run_id, "run_id": run_id, "run_attempt": 1, "name": "CI job"} for run_id in range(300, 306)
            ],
        },
        job_name="CI job",
        roles=("test",),
    )
    active = 0
    peak_active = 0
    lock = threading.Lock()
    two_workers_active = threading.Event()
    release_workers = threading.Event()

    def query(_: str) -> StatusResponse:
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            if active >= 2:
                two_workers_active.set()
        if not release_workers.wait(timeout=2):
            raise TimeoutError("test did not release bounded worker")
        with lock:
            active -= 1
        return StatusResponse(({"invocationStatus": "PARTIAL_INVOCATION_STATUS"},))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as driver:
        collection = driver.submit(
            collect_statuses,
            requests,
            query=query,
            max_workers=2,
            query_mode="test",
            observed_at="2026-10-30T12:30:00Z",
        )
        two_started = two_workers_active.wait(timeout=1)
        release_workers.set()
        records = collection.result(timeout=3)

    assert len(records) == 6
    assert two_started
    assert peak_active == 2
    assert {record["state"] for record in records} == {"in_progress"}


def test_saved_response_identity_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match"):
        parse_status_response({"invocation_id": "another-id", "invocation": []}, invocation="requested-id")
    with pytest.raises(ValueError, match="response item invocationId"):
        parse_status_response(
            {"invocation": [{"invocationId": "another-id", "invocationStatus": "COMPLETE_INVOCATION_STATUS"}]},
            invocation="requested-id",
        )


def test_refresh_overwrites_old_status_with_offline_query_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_snapshot = {
        "runs": [
            {
                "id": 400,
                "run_attempt": 1,
                "event": "push",
                "created_at": "2026-10-30T10:00:00Z",
                "head_sha": "actions-head",
            }
        ],
        "jobs": [{"id": 401, "run_id": 400, "run_attempt": 1, "name": "CI job"}],
    }
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(run_snapshot))
    output_path = tmp_path / "census.json"
    output_path.write_text('{"invocations": [{"state": "complete"}]}')
    responses = tmp_path / "responses"
    responses.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "invocation_census",
            "--snapshot",
            str(snapshot_path),
            "--job-name",
            "CI job",
            "--roles",
            "race",
            "--offline-responses-dir",
            str(responses),
            "--output",
            str(output_path),
        ],
    )

    with pytest.raises(SystemExit) as result:
        invocation_census.main()

    assert result.value.code == 1
    report = json.loads(output_path.read_text())
    assert report["invocations"][0]["state"] == "query_error"
    assert report["invocations"][0]["query_error"] == "offline_response_missing"


if __name__ == "__main__":
    pytest_bazel.main()
