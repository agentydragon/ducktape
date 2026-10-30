import pytest
import pytest_bazel

from devinfra.ci.skills.ci_latency.scripts import analyze_executions

OBSERVED_AT = "2026-10-30T23:00:00Z"


def _execution(execution_id, mnemonic, stage, **timestamps):
    return {
        "executionId": execution_id,
        "actionMnemonic": mnemonic,
        "stage": stage,
        "executedActionMetadata": timestamps,
    }


def test_deduplicates_ids_prefers_complete_rows_and_keeps_outer_inner_stages_distinct():
    invocation_export = {
        "pages": [
            {"invocation": [{"invocationId": "inv-a"}, {"invocationId": "inv-b"}]},
            {"invocation": [{"invocationId": "inv-b"}]},
        ]
    }
    execution_exports = [
        {
            "response": {
                "execution": [
                    _execution(
                        "outer-shared",
                        "RemoteBazelRun",
                        "EXECUTING",
                        queuedTimestamp="1970-01-01T00:00:00Z",
                        workerStartTimestamp="1970-01-01T00:00:00Z",
                    ),
                    _execution(
                        "inner-live",
                        "TestRunner",
                        "EXECUTING",
                        queuedTimestamp="2026-10-30T22:00:00Z",
                        workerStartTimestamp="2026-10-30T22:00:10Z",
                    ),
                ]
            }
        },
        {"response": {}},
        {
            "execution": [
                _execution(
                    "outer-shared",
                    "RemoteBazelRun",
                    "COMPLETED",
                    queuedTimestamp="2026-10-30T22:01:00Z",
                    workerStartTimestamp="2026-10-30T22:01:02Z",
                    executionStartTimestamp="2026-10-30T22:01:03Z",
                    executionCompletedTimestamp="2026-10-30T22:01:08Z",
                ),
                _execution(
                    "inner-done",
                    "TestRunner",
                    "COMPLETED",
                    queuedTimestamp="2026-10-30T22:02:00Z",
                    workerStartTimestamp="2026-10-30T22:02:02Z",
                    executionStartTimestamp="2026-10-30T22:02:07Z",
                    executionCompletedTimestamp="2026-10-30T22:02:10Z",
                ),
            ]
        },
    ]

    result = analyze_executions.analyze(invocation_export, execution_exports, OBSERVED_AT)

    assert result["input"] == {
        "invocation_rows": 3,
        "unique_invocations": 2,
        "duplicate_invocation_rows": 1,
        "execution_exports": 3,
        "execution_rows": 4,
        "unique_execution_ids": 3,
        "duplicate_execution_rows_removed": 1,
    }
    assert result["stage_counts"] == [
        {"action_mnemonic": "RemoteBazelRun", "stage": "COMPLETED", "unique_execution_count": 1},
        {"action_mnemonic": "TestRunner", "stage": "COMPLETED", "unique_execution_count": 1},
        {"action_mnemonic": "TestRunner", "stage": "EXECUTING", "unique_execution_count": 1},
    ]
    assert result["action_spans"]["queue"]["RemoteBazelRun"]["sum_action_seconds"] == 2
    assert result["action_spans"]["worker_preparation"]["RemoteBazelRun"]["sum_action_seconds"] == 1
    assert result["action_spans"]["worker_preparation"]["TestRunner"]["known_count"] == 1
    assert result["action_spans"]["worker_preparation"]["TestRunner"]["unknown_reasons"] == {"end_missing_timestamp": 1}
    assert result["interpretation"]["worker_occupancy_inferred"] is False
    assert result["interpretation"]["capacity_or_quota_inferred"] is False


def test_observation_time_does_not_turn_live_partial_age_into_occupancy():
    result = analyze_executions.analyze(
        {"invocation": [{"invocationId": "inv-stale", "createdAtUsec": "1793400000000000"}]},
        [
            {
                "execution": [
                    _execution(
                        "partial",
                        "Compile",
                        "EXECUTING",
                        queuedTimestamp="2026-10-29T00:00:00Z",
                        workerStartTimestamp="2026-10-29T00:00:10Z",
                    )
                ]
            }
        ],
        OBSERVED_AT,
    )

    queue = result["action_spans"]["queue"]["Compile"]
    preparation = result["action_spans"]["worker_preparation"]["Compile"]
    assert queue["sum_action_seconds"] == 10
    assert queue["known_count"] == 1
    assert preparation["known_count"] == 0
    assert preparation["unknown_reasons"] == {"end_missing_timestamp": 1}
    assert result["interpretation"]["partial_rows_are_not_aged_from_observed_at"] is True
    assert "occupancy_seconds" not in result


def test_epoch_invalid_future_and_non_monotonic_clocks_are_unknown():
    exports = [
        {
            "execution": [
                _execution(
                    "epoch",
                    "Compile",
                    "COMPLETED",
                    queuedTimestamp="1970-01-01T00:00:00Z",
                    workerStartTimestamp="2026-10-30T22:00:00Z",
                    executionStartTimestamp="1970-01-01T00:00:00Z",
                ),
                _execution(
                    "invalid",
                    "Compile",
                    "COMPLETED",
                    queuedTimestamp="not-a-clock",
                    workerStartTimestamp="2026-10-30T22:00:00Z",
                    executionStartTimestamp="nope",
                ),
                _execution(
                    "reverse",
                    "Compile",
                    "COMPLETED",
                    queuedTimestamp="2026-10-30T22:03:00Z",
                    workerStartTimestamp="2026-10-30T22:02:00Z",
                    executionStartTimestamp="2026-10-30T22:01:00Z",
                ),
                _execution(
                    "future",
                    "Compile",
                    "EXECUTING",
                    queuedTimestamp="2026-10-30T23:01:00Z",
                    workerStartTimestamp="2026-10-30T23:02:00Z",
                ),
            ]
        }
    ]

    result = analyze_executions.analyze({"invocation": [{"invocationId": "inv"}]}, exports, OBSERVED_AT)

    queue = result["action_spans"]["queue"]["Compile"]
    preparation = result["action_spans"]["worker_preparation"]["Compile"]
    assert queue["known_count"] == 0
    assert queue["sum_action_seconds"] is None
    assert queue["mean_seconds"] is None
    assert queue["unknown_count"] == 4
    assert queue["unknown_reasons"] == {
        "non_monotonic_timestamps": 1,
        "start_after_observed_at": 1,
        "start_epoch_timestamp": 1,
        "start_invalid_timestamp": 1,
    }
    assert preparation["unknown_reasons"] == {
        "end_epoch_timestamp": 1,
        "end_invalid_timestamp": 1,
        "non_monotonic_timestamps": 1,
        "start_after_observed_at": 1,
    }


def test_accepts_empty_invocation_responses_but_rejects_malformed_nonempty_objects():
    empty_invocation_documents: list[object] = [
        {},
        {"pages": [{}, {"nextPageToken": "", "totalCount": 0}, {"invocation": []}]},
        {"invocation": []},
    ]
    for invocation_document in empty_invocation_documents:
        result = analyze_executions.analyze(invocation_document, [{"response": {}}], OBSERVED_AT)
        assert result["input"]["unique_invocations"] == 0
        assert result["input"]["unique_execution_ids"] == 0

    with pytest.raises(ValueError, match="invocation list"):
        analyze_executions.analyze({"unexpected": "value"}, [{"execution": []}], OBSERVED_AT)
    with pytest.raises(ValueError, match="missing 'invocation' list"):
        analyze_executions.analyze({"pages": [{"unexpected": "value"}]}, [{"execution": []}], OBSERVED_AT)


def test_requires_unique_ids_and_an_explicit_utc_observation_time():
    with pytest.raises(ValueError, match="missing executionId"):
        analyze_executions.analyze(
            {"invocation": [{"invocationId": "inv"}]}, [{"execution": [{"stage": "COMPLETED"}]}], OBSERVED_AT
        )
    with pytest.raises(ValueError, match="conflicting actionMnemonic"):
        analyze_executions.analyze(
            {"invocation": [{"invocationId": "inv"}]},
            [
                {"execution": [_execution("same", "Compile", "COMPLETED")]},
                {"execution": [_execution("same", "Link", "COMPLETED")]},
            ],
            OBSERVED_AT,
        )
    with pytest.raises(ValueError, match="RFC3339 UTC"):
        analyze_executions.analyze({"invocation": []}, [{"execution": []}], "2026-10-30T16:00:00-07:00")
    with pytest.raises(ValueError, match="RFC3339 UTC"):
        analyze_executions.analyze({"invocation": []}, [{"execution": []}], "2026-10-30 23:00:00Z")


if __name__ == "__main__":
    pytest_bazel.main()
