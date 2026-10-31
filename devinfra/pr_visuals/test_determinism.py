import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel

from devinfra.pr_visuals.determinism import (
    Findings,
    Observation,
    Observations,
    Render,
    analyze,
    load_manifest,
    main,
    observe,
    observe_targets,
    report,
    run_once,
    visual_fleet,
)
from util.visual_review import VisualReviewAsset, VisualReviewManifest


def _listing(by_invocation: dict[str, list[dict[str, object]]]):
    """`bbapi artifact list <invocation> --kind test --json`, answered per invocation."""

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(by_invocation[str(command[3])]), "")

    return fake_run


def _artifact(label: str, name: str, uri: str) -> dict[str, object]:
    return {"id": {"testResult": {"label": label}}, "file": {"name": f"test.outputs/{name}", "uri": uri}}


def _manifest(*names: str) -> VisualReviewManifest:
    return VisualReviewManifest(title="Test UI", assets=[VisualReviewAsset(path=name, label=name) for name in names])


@pytest.mark.parametrize(
    ("uris", "drifted", "missing"),
    [
        pytest.param(["same", "same", "same"], False, False, id="stable"),
        pytest.param(["same", "same", "different"], True, False, id="drift"),
        pytest.param(["same", None, "same"], False, True, id="missing-in-one-run"),
        pytest.param([None, None, None], False, True, id="declared-but-never-produced"),
    ],
)
def test_review_assets_require_identical_bytes_in_every_run(
    uris: list[str | None], drifted: bool, missing: bool
) -> None:
    listings = {
        f"run-{i}": [_artifact("//ui:visual", "visual-review.json", "manifest")]
        + ([] if uri is None else [_artifact("//ui:visual", "list.png", uri)])
        for i, uri in enumerate(uris)
    }
    observations = observe(
        list(listings), bbapi=Path("bbapi"), run=_listing(listings), read_manifest=lambda _: _manifest("list.png")
    )
    render = Render("//ui:visual", "list.png")
    assert analyze(observations.review, runs=3) == Findings(
        drifted=[render] if drifted else [], missing=[render] if missing else []
    )
    assert observations.review[render].runs_present == sum(uri is not None for uri in uris)
    assert observations.diagnostics == {}


def test_unpublished_pngs_are_diagnostics_and_other_outputs_are_ignored() -> None:
    observations = observe(
        ["run-1", "run-2"],
        bbapi=Path("bbapi"),
        run=_listing(
            {
                "run-1": [
                    _artifact("//ui:visual", "loading.png", "first"),
                    _artifact("//ui:visual", "trace.zip", "trace"),
                ],
                "run-2": [_artifact("//ui:visual", "loading.png", "second")],
            }
        ),
        read_manifest=lambda _: pytest.fail("no manifest was listed"),
    )
    assert observations.review == {}
    assert observations.diagnostics == {
        Render("//ui:visual", "loading.png"): Observation(by_digest={"first": ["run-1"], "second": ["run-2"]})
    }
    diagnostic_section = report(observations, ["run-1", "run-2"], {}).split("## Diagnostic PNGs")[1]
    assert "loading.png" in diagnostic_section


@pytest.mark.parametrize("name", ["loading.png", "failure.debug.png", "list.png"])
def test_manifest_membership_not_filename_decides_what_is_reviewed(name: str) -> None:
    observations = observe(
        ["run-1", "run-2"],
        bbapi=Path("bbapi"),
        run=_listing(
            {
                invocation: [
                    _artifact("//ui:visual", "visual-review.json", "manifest"),
                    _artifact("//ui:visual", name, invocation),
                ]
                for invocation in ("run-1", "run-2")
            }
        ),
        read_manifest=lambda _: _manifest(name),
    )
    assert analyze(observations.review, runs=2).drifted == [Render("//ui:visual", name)]
    assert observations.diagnostics == {}


@pytest.mark.parametrize("second_manifest", [[], [_artifact("//ui:visual", "visual-review.json", "other")]])
def test_a_png_without_its_own_runs_declaration_is_missing(second_manifest: list[dict[str, object]]) -> None:
    png = _artifact("//ui:visual", "list.png", "same")
    manifests = {"first": _manifest("list.png"), "other": _manifest("other.png")}
    observations = observe(
        ["run-1", "run-2"],
        bbapi=Path("bbapi"),
        run=_listing(
            {"run-1": [_artifact("//ui:visual", "visual-review.json", "first"), png], "run-2": [*second_manifest, png]}
        ),
        read_manifest=manifests.__getitem__,
    )
    render = Render("//ui:visual", "list.png")
    assert render in analyze(observations.review, runs=2).missing
    assert observations.review[render].runs_present == 1


def test_shard_manifests_are_unioned_without_counting_duplicate_outputs_as_runs() -> None:
    first = _artifact("//ui:visual", "list.png", "same")
    second = _artifact("//ui:visual", "detail.png", "detail")
    manifests = {"first": _manifest("list.png"), "second": _manifest("detail.png")}
    manifest_artifacts = [_artifact("//ui:visual", "visual-review.json", uri) for uri in manifests]
    observations = observe(
        ["run-1", "run-2"],
        bbapi=Path("bbapi"),
        run=_listing({"run-1": [*manifest_artifacts, first, first, second], "run-2": [*manifest_artifacts, second]}),
        read_manifest=manifests.__getitem__,
    )
    assert analyze(observations.review, runs=2) == Findings(drifted=[], missing=[Render("//ui:visual", "list.png")])
    assert observations.review[Render("//ui:visual", "list.png")].runs_present == 1
    assert observations.review[Render("//ui:visual", "detail.png")].runs_present == 2


def test_conflicting_outputs_in_one_run_do_not_hide_a_missing_run() -> None:
    observations = {Render("//ui:visual", "list.png"): Observation(by_digest={"first": ["run-1"], "second": ["run-1"]})}
    render = Render("//ui:visual", "list.png")
    assert analyze(observations, runs=2) == Findings(drifted=[render], missing=[render])


def test_manifest_is_read_through_bbapi_blob_cat() -> None:
    manifest = _manifest("loading.png")
    seen: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, manifest.model_dump_json(by_alias=True), "")

    assert load_manifest("bytestream://manifest", run=fake_run) == manifest
    assert seen == [["bbapi", "--timeout", "30s", "blob", "cat", "bytestream://manifest"]]


def test_unreadable_manifest_is_not_silently_treated_as_diagnostic() -> None:
    def fail(_uri: str) -> VisualReviewManifest:
        raise OSError("manifest unavailable")

    with pytest.raises(OSError, match="manifest unavailable"):
        observe(
            ["run-1"],
            bbapi=Path("bbapi"),
            run=_listing({"run-1": [_artifact("//ui:visual", "visual-review.json", "manifest")]}),
            read_manifest=fail,
        )


def test_manifest_membership_is_scoped_to_its_target() -> None:
    observations = observe(
        ["run-1"],
        bbapi=Path("bbapi"),
        run=_listing(
            {
                "run-1": [
                    _artifact("//ui:visual", "visual-review.json", "manifest"),
                    _artifact("//ui:visual", "list.png", "review"),
                    _artifact("//other:browser", "list.png", "diagnostic"),
                ]
            }
        ),
        read_manifest=lambda _: _manifest("list.png"),
    )
    assert set(observations.review) == {Render("//ui:visual", "list.png")}
    assert set(observations.diagnostics) == {Render("//other:browser", "list.png")}


def test_run_once_hands_the_invocation_id_to_bbr_rather_than_reading_it_back() -> None:
    """The run's identity is minted here, so nothing has to parse bbr's console output."""
    seen: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, "", "")

    execution = run_once(["//props/frontend:visual"], bbr=Path("bbr"), run=fake_run)

    assert f"--invocation_id={execution.invocation}" in seen[0]
    assert UUID(execution.invocation).version == 4
    assert execution.returncode == 0
    # Test results must be fresh while prerequisite builds retain remote cache hits.
    assert "--nocache_test_results" in seen[0]
    assert "--noremote_accept_cached" not in seen[0]


def test_the_fleet_is_read_as_records_out_of_the_runner_s_own_progress() -> None:
    """The runner shares this stdout, and closes its last coloured line without a newline.

    So the first target arrives with an escape sequence glued to its front. Reading labels as
    text would drop it -- it does not start with "//" -- and silently check one target fewer.
    A JSON record per line is instead something a log line cannot imitate.
    """
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
    """Only rule records name something runnable; other record types are not the fleet."""
    stdout = (
        '{"type":"SOURCE_FILE","sourceFile":{"name":"//props/frontend:harness.mjs"}}\n'
        '{"type":"RULE","rule":{"name":"//props/frontend:visual","ruleClass":"py_test"}}\n'
    )

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")

    assert visual_fleet(bbr=Path("bbr"), run=fake_run) == ["//props/frontend:visual"]


def test_a_fleet_query_that_matches_nothing_is_an_error_not_an_empty_sweep() -> None:
    """Zero targets would otherwise report "all renders reproduced" having rendered none."""

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "Loading: 0 packages loaded\n", "")

    with pytest.raises(SystemExit):
        visual_fleet(bbr=Path("bbr"), run=fake_run)


def _test_row(label: str, *, status: str = "PASSED", shards: int | None = None, seconds: float) -> dict[str, object]:
    summary: dict[str, object] = {
        "firstStartTime": "2026-09-12T09:29:00.000Z",
        "lastStopTime": f"2026-09-12T09:29:{seconds:06.3f}Z",
    }
    if shards is not None:
        summary["shardCount"] = shards
    return {"metadata": {"label": label}, "status": status, "testSummary": summary}


def test_durations_come_from_buildbuddy_as_wall_time_not_summed_shards() -> None:
    """A sharded target's four 10s shards are 10s of timeout budget, not 40s."""
    listing = {
        "targetGroups": [
            {
                "targets": [
                    # The build row for the same target: no test summary, nothing to time.
                    {"metadata": {"label": "//ui:visual"}, "status": "BUILT"},
                    _test_row("//ui:visual", shards=4, seconds=11.1),
                ]
            }
        ]
    }

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")

    targets = observe_targets(["run-1"], ["//ui:visual"], bbapi=Path("bbapi"), run=fake_run)

    assert [test_run.summary.shard_count for test_run in targets.runs["//ui:visual"]] == [4]
    assert [test_run.summary.wall_time for test_run in targets.runs["//ui:visual"]] == [timedelta(seconds=11.1)]


def test_a_target_the_bulk_listing_truncated_is_still_timed() -> None:
    """BuildBuddy capped a fifteen-target sweep at twelve per group, with no page token.

    The three it dropped were the last alphabetically, so the durations table silently omitted
    them. Anything the sweep ran and the listing did not mention is asked for by label.
    """
    asked: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        flags = [str(part) for part in command]
        asked.append(flags)
        label = flags[flags.index("--label") + 1] if "--label" in flags else "//ui:early"
        payload = {"targetGroups": [{"targets": [_test_row(label, seconds=9.0)]}]}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    timed = observe_targets(["run-1"], ["//ui:early", "//zz:late"], bbapi=Path("bbapi"), run=fake_run)

    assert sorted(timed.runs) == ["//ui:early", "//zz:late"]
    # Only the one the bulk listing missed costs an extra call.
    assert [flags for flags in asked if "--label" in flags] == [
        ["bbapi", "target", "run-1", "--json", "--label", "//zz:late"]
    ]


def test_a_target_that_did_not_pass_says_so_in_the_report() -> None:
    """Nothing dispatches on the status, but a reader comparing renders needs to see it."""

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        label = "//ui:visual"
        payload = {
            "targetGroups": [
                {"targets": [_test_row(label, status="PASSED" if "run-1" in command else "FLAKY", seconds=9.0)]}
            ]
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    observed = observe_targets(["run-1", "run-2"], ["//ui:visual"], bbapi=Path("bbapi"), run=fake_run)
    summary = report(Observations(review={}, diagnostics={}), ["run-1", "run-2"], observed.runs)

    assert "FLAKY, PASSED" in summary


def _exit_status() -> int | str | None:
    """What `main()` raises through `SystemExit`; `None` when it returns, which is exit status 0."""
    try:
        main()
    except SystemExit as exited:
        return exited.code
    return None


@pytest.mark.parametrize(
    ("published_uris", "publish", "status", "returncode", "exit_code"),
    [
        pytest.param(["same", "same", "same"], True, "PASSED", 0, None, id="stable-review-drifting-diagnostics"),
        pytest.param(["first", "second", "third"], True, "PASSED", 0, 1, id="drifted-review"),
        pytest.param([None, "same", "same"], True, "PASSED", 0, 1, id="missing-review"),
        pytest.param([None, None, None], True, "PASSED", 0, 1, id="never-produced-review"),
        pytest.param([None, None, None], False, "PASSED", 0, None, id="diagnostics-only"),
        pytest.param(["same", "same", "same"], True, "FAILED", 0, 1, id="failed-test-with-stable-review"),
        pytest.param([None, None, None], False, "FAILED", 0, 1, id="failed-diagnostic-only-test"),
        pytest.param(["same", "same", "same"], True, "FLAKY", 0, 1, id="flaky-test"),
        pytest.param(["same", "same", "same"], True, "PASSED", 1, 1, id="bbr-failure-with-artifacts"),
        pytest.param([None, None, None], False, None, 1, 1, id="build-failure-without-test-results"),
        pytest.param([None, None, None], False, None, 0, 1, id="no-test-results"),
    ],
)
def test_exit_status_preserves_review_and_test_failures_but_not_diagnostic_drift(
    monkeypatch: pytest.MonkeyPatch,
    published_uris: list[str | None],
    publish: bool,
    status: str | None,
    returncode: int,
    exit_code: int | None,
) -> None:
    invocations: list[str] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload: object = None
        match [str(part) for part in command]:
            case ["bbr", "test", flag, *_]:
                invocations.append(flag.removeprefix("--invocation_id="))
                return subprocess.CompletedProcess(command, returncode, "", "")
            case ["bbapi", "artifact", "list", invocation, "--kind", "test", "--json"]:
                uri = published_uris[invocations.index(invocation)]
                artifacts = [_artifact("//ui:visual", "loading.png", invocation)]
                if invocation == invocations[0]:
                    artifacts.append(_artifact("//ui:visual", "transient.png", "first-run-only"))
                if publish:
                    artifacts.append(_artifact("//ui:visual", "visual-review.json", "manifest"))
                if uri is not None:
                    artifacts.append(_artifact("//ui:visual", "list.png", uri))
                payload = artifacts
            case ["bbapi", "target", _, "--json", *_]:
                targets = [] if status is None else [_test_row("//ui:visual", status=status, seconds=9.0)]
                payload = {"targetGroups": [{"targets": targets}]}
            case other:
                raise AssertionError(f"unexpected command {other}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload) if payload is not None else "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("devinfra.pr_visuals.determinism.load_manifest", lambda _, **_kwargs: _manifest("list.png"))
    monkeypatch.setattr(sys, "argv", ["determinism", "--runs", str(len(published_uris)), "//ui:visual"])

    assert _exit_status() == exit_code


@pytest.mark.parametrize(
    ("run_count", "unavailable_indexes", "launcher_exit", "observed_count"),
    [
        pytest.param(3, (1,), 19, 2, id="failed-launcher-among-passing-runs"),
        pytest.param(2, (1,), 0, 1, id="missing-invocation-after-zero-exit"),
        pytest.param(2, (0, 1), 19, 0, id="all-attempts-have-no-invocation"),
    ],
)
def test_unstarted_commands_are_reported_without_discarding_observations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_count: int,
    unavailable_indexes: tuple[int, ...],
    launcher_exit: int,
    observed_count: int,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        words = tuple(str(part) for part in command)
        match words:
            case ("bbr", "test", *_):
                commands.append(words)
                index = len(commands) - 1
                returncode = launcher_exit if index in unavailable_indexes else 0
                return subprocess.CompletedProcess(command, returncode, "", "remote runner unavailable")
            case ("bbapi", "target", invocation, "--json", *_):
                invocation_index = next(
                    index
                    for index, words in enumerate(commands)
                    if words[2].removeprefix("--invocation_id=") == invocation
                )
                if invocation_index in unavailable_indexes:
                    return subprocess.CompletedProcess(command, 1, "", "Error: invocation not found")
                payload = {"targetGroups": [{"targets": [_test_row("//ui:visual", seconds=9.0)]}]}
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            case ("bbapi", "artifact", "list", invocation, "--kind", "test", "--json"):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        [
                            _artifact("//ui:visual", "visual-review.json", "manifest"),
                            _artifact("//ui:visual", "list.png", "stable-image"),
                        ]
                    ),
                    "",
                )
            case other:
                raise AssertionError(f"unexpected command {other}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("devinfra.pr_visuals.determinism.load_manifest", lambda _, **_kwargs: _manifest("list.png"))
    monkeypatch.setattr(sys, "argv", ["determinism", "--runs", str(run_count), "//ui:visual"])

    assert _exit_status() == 1

    output = capsys.readouterr().out
    assert len(commands) == run_count
    assert f"# Visual determinism over {observed_count} observed runs" in output
    assert f"{run_count} commands were attempted; {observed_count} produced BuildBuddy invocations." in output
    if observed_count >= 2:
        assert "All 1 image(s) reproduced identically in every run." in output
    else:
        assert f"Insufficient observed runs: {observed_count}; at least 2 are required" in output
        assert "reproduced identically" not in output

    for index in unavailable_indexes:
        failed_command = commands[index]
        invocation = failed_command[2].removeprefix("--invocation_id=")
        assert f"`{invocation}`: BuildBuddy has no invocation record; bbr exited {launcher_exit}." in output
        assert "Command: `bbr test " + " ".join(failed_command[2:]) + "`" in output


if __name__ == "__main__":
    pytest_bazel.main()
