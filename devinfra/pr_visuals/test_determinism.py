from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel

from devinfra.pr_visuals.collection import (
    CollectedOutputs,
    CollectionIssue,
    OutputKey,
    OutputObservation,
    ResultCoverage,
    ResultKey,
)
from devinfra.pr_visuals.determinism import (
    Execution,
    Findings,
    Render,
    Roster,
    analyze,
    from_collection,
    observe,
    observe_targets,
    report,
    resolve_test_targets,
    run_once,
    visual_fleet,
)
from devinfra.pr_visuals.targets import TestRun as TargetRun, TestSummary as TargetTestSummary
from devinfra.pr_visuals.testing.outputs import output_observation

_TARGET = "//ui:visual"
_REPOSITORY = "agentydragon/ducktape"


def _test_run(label: str = _TARGET, *, status: str = "PASSED", seconds: float = 9) -> TargetRun:
    summary = TargetTestSummary.model_validate(
        {
            "shardCount": 1,
            "firstStartTime": "2026-09-12T09:29:00.000Z",
            "lastStopTime": f"2026-09-12T09:29:{seconds:06.3f}Z",
        }
    )
    return TargetRun(label, status, summary)


def _coverage(
    invocation: str,
    *,
    target: str = _TARGET,
    configuration: str | None = "config-a",
    run: int | None = 1,
    shard: int | None = 1,
    attempt: int | None = 1,
    selected: bool = True,
    issues: tuple[CollectionIssue, ...] = (),
    retried_by: int | None = None,
) -> ResultCoverage:
    result = ResultKey(invocation, target, configuration, run, shard, attempt)
    return ResultCoverage(result, "PASSED" if selected else "FAILED", "PASSED", 1, selected, issues, retried_by)


def _output(
    invocation: str,
    contents: str,
    *,
    target: str = _TARGET,
    case_id: str = "dialog/mobile",
    checkpoint: str | None = "expanded",
    role: str = "screenshot",
    configuration: str = "config-a",
    run: int = 1,
    shard: int = 1,
    attempt: int = 1,
    label: str | None = "Expanded dialog",
) -> OutputObservation:
    result = ResultKey(invocation, target, configuration, run, shard, attempt)
    key = OutputKey(_REPOSITORY, target, case_id, checkpoint, role)
    return output_observation(contents.encode(), key=key, result=result, label=label)


def _collection(
    invocations: list[str],
    *,
    outputs: tuple[OutputObservation, ...] = (),
    results: tuple[ResultCoverage, ...] | None = None,
    issues: tuple[CollectionIssue, ...] = (),
    complete: bool = True,
) -> CollectedOutputs:
    if results is None:
        results = tuple(_coverage(invocation) for invocation in invocations)
    return CollectedOutputs(outputs, outputs, results, issues, complete)


def _target_runs(invocations: list[str], *, status: str = "PASSED") -> dict[str, dict[str, list[TargetRun]]]:
    return {invocation: {_TARGET: [_test_run(status=status)]} for invocation in invocations}


def _noop_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def test_identical_successful_screenshot_observations_compare_by_digest() -> None:
    invocations = ["run-1", "run-2"]
    screenshot = Render(_TARGET, "dialog/mobile", "expanded", "screenshot")
    observed = from_collection(
        _collection(invocations, outputs=(_output("run-1", "same"), _output("run-2", "same"))),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert analyze(observed) == Findings([], [])
    assert observed.screenshots[screenshot].runs_present == 2
    assert observed.screenshots[screenshot].by_digest == {
        hashlib.sha256(b"same").hexdigest(): [
            Roster(_TARGET, "run-1", "config-a", 1),
            Roster(_TARGET, "run-2", "config-a", 1),
        ]
    }


def test_changed_digest_is_drift_for_the_same_logical_identity() -> None:
    invocations = ["run-1", "run-2"]
    screenshot = Render(_TARGET, "dialog/mobile", "expanded", "screenshot")
    observed = from_collection(
        _collection(invocations, outputs=(_output("run-1", "first"), _output("run-2", "second"))),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert analyze(observed) == Findings([screenshot], [])


def test_output_absence_is_reported_only_across_complete_successful_rosters() -> None:
    invocations = ["run-1", "run-2"]
    missing_screenshot = Render(_TARGET, "dialog/desktop", "expanded", "screenshot")
    observed = from_collection(
        _collection(
            invocations,
            outputs=(
                _output("run-1", "same"),
                _output("run-1", "desktop", case_id="dialog/desktop"),
                _output("run-2", "same"),
            ),
        ),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert analyze(observed) == Findings([], [missing_screenshot])
    assert observed.screenshots[Render(_TARGET, "dialog/mobile", "expanded", "screenshot")].runs_present == 2
    assert len(observed.complete_rosters[_TARGET]) == 2
    assert len(observed.complete_rosters[_TARGET]) == 2


def test_failed_target_is_reported_separately_and_not_counted_as_missing() -> None:
    invocations = ["passed", "failed"]
    screenshot = Render(_TARGET, "dialog/mobile", "expanded", "screenshot")
    results = (
        _coverage("passed"),
        _coverage(
            "failed",
            selected=False,
            issues=(CollectionIssue("terminal failed", result=ResultKey("failed", _TARGET, "config-a", 1, 1, 1)),),
        ),
    )
    observed = from_collection(
        _collection(invocations, outputs=(_output("passed", "same"),), results=results, complete=False),
        invocations,
        [_TARGET],
        _target_runs(invocations, status="PASSED") | {"failed": {_TARGET: [_test_run(status="FAILED")]}},
    )

    assert analyze(observed) == Findings([], [])
    assert len(observed.complete_rosters[_TARGET]) == 1
    failures = [issue for issue in observed.incomplete_rosters if "test target failed" in issue.message]
    assert len(failures) == 1
    assert failures[0].invocation == "failed"
    assert failures[0].target == _TARGET
    assert failures[0].result is None
    assert screenshot not in analyze(observed).missing


def test_retry_attempts_are_not_extra_rosters_and_terminal_success_is_used() -> None:
    invocations = ["run-1", "run-2"]
    first_attempt = _coverage("run-1", attempt=1, selected=False, retried_by=2)
    terminal_attempt = _coverage("run-1", attempt=2)
    observed = from_collection(
        _collection(
            invocations,
            outputs=(_output("run-1", "same", attempt=2), _output("run-2", "same")),
            results=(first_attempt, terminal_attempt, _coverage("run-2")),
            issues=(CollectionIssue("retried by attempt 2", result=ResultKey("run-1", _TARGET, "config-a", 1, 1, 1)),),
        ),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert analyze(observed) == Findings([], [])
    assert len(observed.complete_rosters[_TARGET]) == 2
    assert observed.issues == (
        CollectionIssue("retried by attempt 2", result=ResultKey("run-1", _TARGET, "config-a", 1, 1, 1)),
    )


def test_disjoint_shards_form_one_roster_without_counting_each_shard_as_a_run() -> None:
    invocations = ["run-1", "run-2"]
    cases = ["dialog/mobile", "dialog/desktop"]
    outputs = tuple(
        _output(invocation, "same", case_id=case, shard=shard)
        for invocation in invocations
        for shard, case in enumerate(cases, start=1)
    )
    results = tuple(_coverage(invocation, shard=shard) for invocation in invocations for shard in (1, 2))
    observed = from_collection(
        _collection(invocations, outputs=outputs, results=results), invocations, [_TARGET], _target_runs(invocations)
    )

    assert len(observed.complete_rosters[_TARGET]) == 2
    assert len(observed.screenshots) == 2
    assert all(item.runs_present == 2 for item in observed.screenshots.values())
    assert analyze(observed) == Findings([], [])


def test_duplicate_identity_across_shards_invalidates_roster_instead_of_faking_drift() -> None:
    invocations = ["run-1", "run-2"]
    outputs = (_output("run-1", "same", shard=1), _output("run-1", "same", shard=2), _output("run-2", "same", shard=1))
    results = (_coverage("run-1", shard=1), _coverage("run-1", shard=2), _coverage("run-2", shard=1))
    observed = from_collection(
        _collection(invocations, outputs=outputs, results=results), invocations, [_TARGET], _target_runs(invocations)
    )

    assert len(observed.complete_rosters[_TARGET]) == 1
    assert analyze(observed) == Findings([], [])
    assert any("duplicate screenshot identity across shards" in issue.message for issue in observed.incomplete_rosters)


def test_multiple_configurations_fail_closed_without_comparing_digests() -> None:
    invocations = ["run-1", "run-2"]
    results = (_coverage("run-1", configuration="config-a"), _coverage("run-2", configuration="config-b"))
    observed = from_collection(
        _collection(
            invocations,
            outputs=(
                _output("run-1", "first", configuration="config-a"),
                _output("run-2", "second", configuration="config-b"),
            ),
            results=results,
        ),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert observed.complete_rosters == {}
    assert analyze(observed) == Findings([], [])
    assert any("no declared comparison profile" in issue.message for issue in observed.incomplete_rosters)


def test_non_screenshot_roles_are_informational_and_never_diffed() -> None:
    invocations = ["run-1", "run-2"]
    observed = from_collection(
        _collection(
            invocations,
            outputs=(_output("run-1", "first", role="diagnostics"), _output("run-2", "second", role="diagnostics")),
        ),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert observed.screenshots == {}
    assert len(observed.diagnostics) == 2
    assert analyze(observed) == Findings([], [])


@pytest.mark.parametrize("diagnostic_only", [False, True], ids=["no-records", "diagnostics-only"])
def test_complete_visual_roster_without_a_screenshot_fails(diagnostic_only: bool) -> None:
    invocations = ["run-1", "run-2"]
    outputs = (
        (_output("run-1", "trace-a", role="diagnostics"), _output("run-2", "trace-b", role="diagnostics"))
        if diagnostic_only
        else ()
    )
    observed = from_collection(
        _collection(invocations, outputs=outputs), invocations, [_TARGET], _target_runs(invocations)
    )

    assert not observed.complete_rosters.get(_TARGET)
    assert any("emitted no screenshot outputs" in issue.message for issue in observed.incomplete_rosters)
    assert analyze(observed) == Findings([], [])


def test_incomplete_roster_does_not_skip_the_next_valid_roster() -> None:
    invocations = ["empty", "present"]
    observed = from_collection(
        _collection(invocations, outputs=(_output("present", "same"),)),
        invocations,
        [_TARGET],
        _target_runs(invocations),
    )

    assert len(observed.complete_rosters[_TARGET]) == 1
    assert observed.complete_rosters[_TARGET][0].invocation == "present"
    assert next(iter(observed.screenshots.values())).runs_present == 1
    assert any(
        issue.result is not None and issue.result.invocation == "empty" and "no screenshot outputs" in issue.message
        for issue in observed.incomplete_rosters
    )


def test_case_checkpoint_role_and_target_are_all_part_of_render_identity() -> None:
    invocations = ["run-1"]
    outputs = (
        _output("run-1", "a", case_id="case-a", checkpoint=None),
        _output("run-1", "b", case_id="case-a", checkpoint="named"),
        _output("run-1", "c", case_id="case-b", checkpoint="named"),
        _output("run-1", "d", case_id="case-a", checkpoint="named", role="trace"),
        _output("run-1", "e", case_id="case-a", checkpoint="named", target="//other:visual"),
    )
    target_runs = _target_runs(invocations) | {
        "run-1": {_TARGET: [_test_run()], "//other:visual": [_test_run("//other:visual")]}
    }
    results = (_coverage("run-1"), _coverage("run-1", target="//other:visual"))
    observed = from_collection(
        _collection(invocations, outputs=outputs, results=results),
        invocations,
        [_TARGET, "//other:visual"],
        target_runs,
    )

    assert set(observed.screenshots) == {
        Render(_TARGET, "case-a", None, "screenshot"),
        Render(_TARGET, "case-a", "named", "screenshot"),
        Render(_TARGET, "case-b", "named", "screenshot"),
        Render("//other:visual", "case-a", "named", "screenshot"),
    }
    assert len(observed.diagnostics) == 1


def test_report_separates_unavailable_invocation_from_missing_screenshots() -> None:
    invocation = "present"
    observed = from_collection(
        _collection([invocation], outputs=(_output(invocation, "same"),)),
        [invocation],
        [_TARGET],
        _target_runs([invocation]),
    )
    executions = [
        Execution("missing", ("bbr", "test", "--invocation_id=missing"), 19),
        Execution(invocation, ("bbr", "test", "--invocation_id=present"), 0),
    ]

    summary = report(
        observed, [invocation], {_TARGET: [_test_run()]}, executions=executions, unavailable_invocations=["missing"]
    )

    assert "## Failed or unavailable invocations" in summary
    assert "BuildBuddy has no invocation record" in summary
    assert "Insufficient complete repeated runs" in summary
    assert "absent from complete rosters" not in summary
    assert "invocation 1: BuildBuddy has no invocation record; bbr exited 19." in summary
    assert "Command: `bbr test --invocation_id=missing`" in summary
    assert "[invocation 1](https://app.buildbuddy.io/invocation/missing)" not in summary
    assert "[invocation 2](https://app.buildbuddy.io/invocation/present)" in summary


def test_report_links_compact_result_refs_and_aggregates_shards() -> None:
    invocation = "run/with space"
    shared_config_prefix = "12345678"
    issues = (
        *(
            CollectionIssue(
                "repeated collector issue",
                result=ResultKey(invocation, _TARGET, "abcdef0123456789", 1, shard, 2),
                record="test.outputs/test-outputs/records/duplicate.json",
            )
            for shard in (1, 2, 3, 5)
        ),
        CollectionIssue(
            "first configuration issue", result=ResultKey(invocation, _TARGET, shared_config_prefix + "aaaa", 1, 6, 1)
        ),
        CollectionIssue(
            "second configuration issue", result=ResultKey(invocation, _TARGET, shared_config_prefix + "bbbb", 1, 7, 1)
        ),
        CollectionIssue(
            "output identity also claimed across shards",
            result=ResultKey(invocation, _TARGET, "config-a", 1, 2, 1),
            related_results=(ResultKey(invocation, _TARGET, "config-a", 1, 1, 1),),
            output=OutputKey(_REPOSITORY, _TARGET, "dialog/mobile", None, "screenshot"),
        ),
    )
    observed = from_collection(
        _collection([invocation], outputs=(_output(invocation, "same"),), issues=issues),
        [invocation],
        [_TARGET],
        _target_runs([invocation]),
    )
    summary = report(observed, [invocation], {_TARGET: [_test_run()]})

    assert "[invocation 1](https://app.buildbuddy.io/invocation/run%2Fwith%20space)" in summary
    assert (
        "shards 1-3, 5, attempt 2, record `test.outputs/test-outputs/records/duplicate.json`: repeated collector issue"
    ) in summary
    assert "shards 1-2, attempt 1, case `dialog/mobile`, checkpoint `<default>`, role `screenshot`" in summary
    assert "config `12345678a…`" in summary
    assert "config `12345678b…`" in summary
    assert "abcdef0123456789" not in summary
    assert "12345678aaaa" not in summary
    assert "12345678bbbb" not in summary
    assert "ResultKey(" not in summary
    assert "OutputKey(" not in summary


def test_report_numbers_available_invocations_missing_from_execution_rows() -> None:
    invocation = "available-only"
    observed = from_collection(
        _collection([invocation], outputs=(_output(invocation, "same"),)),
        [invocation],
        [_TARGET],
        _target_runs([invocation]),
    )

    summary = report(
        observed,
        [invocation],
        {_TARGET: [_test_run()]},
        executions=[Execution("different-attempt", ("bbr", "test"), 0)],
    )

    assert "2. [invocation 2](https://app.buildbuddy.io/invocation/available-only)" in summary


def test_observe_passes_expected_targets_to_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    collected = _collection(["run-1"])

    def fake_collect(invocations: list[str], repository: str, destination: Path, **kwargs: object) -> CollectedOutputs:
        seen["invocations"] = invocations
        seen["repository"] = repository
        seen["destination"] = destination
        seen.update(kwargs)
        return collected

    monkeypatch.setattr("devinfra.pr_visuals.determinism.collect_outputs", fake_collect)
    observed = observe(["run-1"], [_TARGET], _target_runs(["run-1"]), bbapi=Path("bbapi"), run=_noop_runner)

    assert seen["targets"] == [_TARGET]
    assert seen["repository"] == _REPOSITORY
    assert isinstance(seen["destination"], Path)
    assert observed.collection_complete


def test_output_collection_error_is_reported_as_incomplete_not_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> CollectedOutputs:
        raise RuntimeError("artifact listing unavailable")

    monkeypatch.setattr("devinfra.pr_visuals.determinism.collect_outputs", fail)
    observed = observe(["run-1"], [_TARGET], _target_runs(["run-1"]), bbapi=Path("bbapi"), run=_noop_runner)

    assert analyze(observed) == Findings([], [])
    assert not observed.collection_complete
    assert observed.issues == (CollectionIssue("output collection failed: artifact listing unavailable"),)


def test_run_once_hands_the_invocation_id_to_bbr_rather_than_reading_it_back() -> None:
    """The run's identity is minted here, so nothing has to parse bbr's console output."""
    seen: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, "", "")

    execution = run_once([_TARGET], bbr=Path("bbr"), run=fake_run)

    assert f"--invocation_id={execution.invocation}" in seen[0]
    assert UUID(execution.invocation).version == 4
    assert execution.returncode == 0
    assert "--nocache_test_results" in seen[0]
    assert "--noremote_accept_cached" not in seen[0]


def test_the_fleet_is_read_as_records_out_of_the_runner_s_own_progress() -> None:
    stdout = (
        "Waiting for available remote runner...\n"
        "\x1b[90m2026-09-12 15:28:14.665 UTC \x1b[mSyncing existing repo...\n"
        '\x1b[m{"type":"RULE","rule":{"name":"//aiquota/frontend:screenshots","ruleClass":"py_test"}}\n'
        '{"type":"RULE","rule":{"name":"//props/frontend:visual","ruleClass":"py_test"}}\n'
        "\x1b[32mINFO: \x1b[mElapsed time: 2.2s\n"
        "Remote run completed at 2026-09-12 15:28:20 UTC\n"
    )

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        flags = [str(part) for part in command]
        assert "--output=streamed_jsonproto" in flags
        assert "attr(tags, visual, //...)" in flags
        return subprocess.CompletedProcess(command, 0, stdout, "")

    assert visual_fleet(bbr=Path("bbr"), run=fake_run) == ["//aiquota/frontend:screenshots", "//props/frontend:visual"]


def test_a_source_file_in_the_query_output_is_not_a_target_to_run() -> None:
    stdout = (
        '{"type":"SOURCE_FILE","sourceFile":{"name":"//props/frontend:harness.mjs"}}\n'
        '{"type":"RULE","rule":{"name":"//props/frontend:visual","ruleClass":"py_test"}}\n'
    )

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")

    assert visual_fleet(bbr=Path("bbr"), run=fake_run) == ["//props/frontend:visual"]


def test_a_fleet_query_that_matches_nothing_is_an_error_not_an_empty_sweep() -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "Loading: 0 packages loaded\n", "")

    with pytest.raises(SystemExit):
        visual_fleet(bbr=Path("bbr"), run=fake_run)


def test_positional_patterns_resolve_to_canonical_test_labels() -> None:
    asked: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = [str(part) for part in command]
        asked.append(words)
        pattern = words[-1]
        if "//ui:" in pattern:
            stdout = '{"type":"RULE","rule":{"name":"//ui:visual","ruleClass":"py_test"}}\n'
        else:
            stdout = '{"type":"RULE","rule":{"name":"//browser:smoke","ruleClass":"py_test"}}\n'
        return subprocess.CompletedProcess(command, 0, stdout, "")

    assert resolve_test_targets(["//ui:visual", "//browser/..."], bbr=Path("bbr"), run=fake_run) == [
        "//browser:smoke",
        "//ui:visual",
    ]
    assert all(command[2] == "--output=streamed_jsonproto" for command in asked)
    assert all(command[-1].startswith('kind(".*_test rule", ') for command in asked)


def test_unmatched_positional_test_pattern_is_an_error() -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "Loading: 0 packages loaded\n", "")

    with pytest.raises(SystemExit, match="matched no test targets"):
        resolve_test_targets(["//empty:all"], bbr=Path("bbr"), run=fake_run)


def _test_row(label: str, *, status: str = "PASSED", shards: int | None = None, seconds: float) -> dict[str, object]:
    summary: dict[str, object] = {
        "firstStartTime": "2026-09-12T09:29:00.000Z",
        "lastStopTime": f"2026-09-12T09:29:{seconds:06.3f}Z",
    }
    if shards is not None:
        summary["shardCount"] = shards
    return {"metadata": {"label": label}, "status": status, "testSummary": summary}


def test_durations_come_from_buildbuddy_as_wall_time_not_summed_shards() -> None:
    listing = {
        "targetGroups": [
            {
                "targets": [
                    {"metadata": {"label": _TARGET}, "status": "BUILT"},
                    _test_row(_TARGET, shards=4, seconds=11.1),
                ]
            }
        ]
    }

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")

    targets = observe_targets(["run-1"], [_TARGET], bbapi=Path("bbapi"), run=fake_run)

    assert [test_run.summary.shard_count for test_run in targets.runs[_TARGET]] == [4]
    assert [test_run.summary.wall_time for test_run in targets.runs[_TARGET]] == [timedelta(seconds=11.1)]
    assert targets.runs_by_invocation["run-1"][_TARGET] == targets.runs[_TARGET]


def test_a_target_the_bulk_listing_truncated_is_still_timed() -> None:
    asked: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        flags = [str(part) for part in command]
        asked.append(flags)
        label = flags[flags.index("--label") + 1] if "--label" in flags else "//ui:early"
        payload = {"targetGroups": [{"targets": [_test_row(label, seconds=9.0)]}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    timed = observe_targets(["run-1"], ["//ui:early", "//zz:late"], bbapi=Path("bbapi"), run=fake_run)

    assert sorted(timed.runs) == ["//ui:early", "//zz:late"]
    assert [flags for flags in asked if "--label" in flags] == [
        ["bbapi", "target", "run-1", "--json", "--label", "//zz:late"]
    ]


def test_unavailable_invocation_is_not_returned_as_a_missing_screenshot_run() -> None:
    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "absent" in command:
            return subprocess.CompletedProcess(command, 1, "", "Error: invocation not found")
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"targetGroups": [{"targets": [_test_row(_TARGET, seconds=9.0)]}]}), ""
        )

    observed = observe_targets(["present", "absent"], [_TARGET], bbapi=Path("bbapi"), run=fake_run)

    assert observed.invocations == ["present"]
    assert observed.unavailable_invocations == ["absent"]
    assert "absent" not in observed.runs_by_invocation


if __name__ == "__main__":
    pytest_bazel.main()
