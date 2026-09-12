import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel

from devinfra.pr_visuals.determinism import (
    Observation,
    Render,
    observe,
    observe_targets,
    report,
    run_once,
    visual_fleet,
)


def _listing(by_invocation: dict[str, list[dict[str, str]]]):
    """`bbapi artifact list <invocation> --json`, answered per invocation."""

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(by_invocation[str(command[3])]), "")

    return fake_run


def _png(label: str, name: str, uri: str) -> dict[str, str]:
    return {"label": label, "name": f"test.outputs/{name}", "uri": uri}


def test_a_render_with_one_digest_across_runs_is_reproducible() -> None:
    """The whole point: same bytes every run means one content-addressed URI every run."""
    artifacts = [_png("//ui:visual", "list.png", "bytestream://same")]
    observations = observe(
        ["run-1", "run-2", "run-3"],
        bbapi=Path("bbapi"),
        run=_listing({"run-1": artifacts, "run-2": artifacts, "run-3": artifacts}),
    )

    assert observations == {
        Render("//ui:visual", "list.png"): Observation(by_digest={"bytestream://same": ["run-1", "run-2", "run-3"]})
    }
    assert "reproduced identically" in report(observations, ["run-1", "run-2", "run-3"], {})


def test_a_render_that_drifts_in_one_run_of_three_is_caught() -> None:
    """The case two runs can miss: a race that fires intermittently."""
    stable = _png("//ui:visual", "list.png", "bytestream://same")
    observations = observe(
        ["run-1", "run-2", "run-3"],
        bbapi=Path("bbapi"),
        run=_listing(
            {"run-1": [stable], "run-2": [stable], "run-3": [_png("//ui:visual", "list.png", "bytestream://drifted")]}
        ),
    )

    summary = report(observations, ["run-1", "run-2", "run-3"], {})
    assert "1 render(s) not reproducible" in summary
    assert "2/3 run(s), first `run-1`" in summary
    assert "1/3 run(s), first `run-3`" in summary


def test_a_render_missing_from_some_runs_is_its_own_finding() -> None:
    """Comparing digests over only the runs that produced it would call this stable."""
    observations = observe(
        ["run-1", "run-2"],
        bbapi=Path("bbapi"),
        run=_listing({"run-1": [_png("//ui:visual", "flaky.png", "bytestream://a")], "run-2": []}),
    )

    summary = report(observations, ["run-1", "run-2"], {})
    assert "not published by every run" in summary
    assert "present in 1/2" in summary


def test_non_png_outputs_are_not_renders() -> None:
    """The manifest is republished every run and its digest legitimately varies."""
    manifest = {"label": "//ui:visual", "name": "test.outputs/visual-review.json", "uri": "bytestream://manifest"}
    observations = observe(["run-1"], bbapi=Path("bbapi"), run=_listing({"run-1": [manifest]}))

    assert observations == {}


def test_run_once_hands_the_invocation_id_to_bbr_rather_than_reading_it_back() -> None:
    """The run's identity is minted here, so nothing has to parse bbr's console output."""
    seen: list[list[str]] = []

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, "", "")

    invocation = run_once(["//props/frontend:visual"], bbr=Path("bbr"), run=fake_run)

    assert f"--invocation_id={invocation}" in seen[0]
    assert UUID(invocation).version == 4
    # Without both, a later run replays the first run's result and every scene looks stable.
    assert "--nocache_test_results" in seen[0]
    assert "--noremote_accept_cached" in seen[0]


def test_the_fleet_is_read_as_records_out_of_the_runner_s_own_progress() -> None:
    """The runner shares this stdout, and closes its last coloured line without a newline.

    So the first target arrives with an escape sequence glued to its front. Reading labels as
    text would drop it -- it does not start with "//" -- and silently check one target fewer.
    A JSON record per line is instead something a log line cannot imitate.
    """
    stdout = (
        "Waiting for available remote runner...\n"
        "\x1b[90m2026-09-12 15:28:14.665 UTC \x1b[mSyncing existing repo...\n"
        '\x1b[m{"type":"RULE","rule":{"name":"//aiquota/frontend:screenshots","ruleClass":"js_test"}}\n'
        '{"type":"RULE","rule":{"name":"//props/frontend:visual","ruleClass":"js_test"}}\n'
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
        '{"type":"RULE","rule":{"name":"//props/frontend:visual","ruleClass":"js_test"}}\n'
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

    assert [test_run.summary.shard_count for test_run in targets["//ui:visual"]] == [4]
    summary = report({}, ["run-1"], targets)
    assert "| `//ui:visual` | 4 | 11.1s | 11.1s | PASSED |" in summary


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

    assert sorted(timed) == ["//ui:early", "//zz:late"]
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

    summary = report(
        {}, ["run-1", "run-2"], observe_targets(["run-1", "run-2"], ["//ui:visual"], bbapi=Path("bbapi"), run=fake_run)
    )

    assert "FLAKY, PASSED" in summary


if __name__ == "__main__":
    pytest_bazel.main()
