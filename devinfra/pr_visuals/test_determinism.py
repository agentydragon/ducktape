import json
import subprocess
from pathlib import Path

import pytest_bazel

from devinfra.pr_visuals.determinism import Observation, Render, durations, observe, report, run_once


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


def test_run_once_reports_the_invocation_and_what_each_target_cost() -> None:
    """bbr announces the invocation only on its closing line; durations come from Bazel's."""
    output = (
        "//props/frontend:visual                        \x1b[32mPASSED \x1b[0min 10.1s\n"
        "//x/agentplane/app/frontend:visual             \x1b[32mPASSED \x1b[0min 14.5s\n"
        "Executed 2 out of 2 tests: 2 tests pass.\n"
        "bbr: invocation 4493681a-c750-49ba-af99-d756df6e956e  (bbapi ...)\n"
    )

    def fake_run(command: list[str | Path], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "--nocache_test_results" in [str(part) for part in command]
        assert "--noremote_accept_cached" in [str(part) for part in command]
        return subprocess.CompletedProcess(command, 0, output, "")

    executed = run_once(["//props/frontend:visual"], bbr=Path("bbr"), run=fake_run)

    assert executed.invocation == "4493681a-c750-49ba-af99-d756df6e956e"
    assert executed.durations == {"//props/frontend:visual": 10.1, "//x/agentplane/app/frontend:visual": 14.5}


def test_durations_ignores_a_line_that_is_not_a_target_result() -> None:
    assert durations("Stats over 6 runs: max = 15.8s, min = 10.1s\n") == {}


if __name__ == "__main__":
    pytest_bazel.main()
