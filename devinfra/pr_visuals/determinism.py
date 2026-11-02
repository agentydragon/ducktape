"""Run visual targets repeatedly and report which renders are not reproducible.

A passing visual test proves the scene rendered, not that it renders the same pixels
twice. Left unchecked, drift is discovered as a misleading "X% changed" on somebody's
unrelated PR, weeks after the scene stopped being stable.

Two runs is the obvious check and is too few: a race that fires one time in five looks
perfectly stable across a pair, and the three instances this repo has already hit (an
unguarded Mantine animation, two mocked-fetch races) are exactly that shape. This runs N.

Only `visual-review.json` manifests are downloaded. Their assets are compared using
BuildBuddy's content-addressed artifact URIs, without downloading PNGs. Other PNGs are
reported as diagnostics and do not fail the sweep. Test failures still fail it.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from devinfra.pr_visuals.artifacts import Runner, TestResultIdentity, list_ci_artifacts
from devinfra.pr_visuals.targets import TestRun, list_test_runs
from util.bazel.workspace import get_build_workspace_directory
from util.visual_review import MANIFEST_NAME, VisualReviewManifest

# Every target that drives a browser carries this tag -- `visual_test` applies it, and the
# screenshot macros set it directly -- so the fleet is a question for Bazel rather than a list
# somebody has to remember to update. It has already gone stale once: four per-scenario airlock
# targets outlived their collapse into one sweep.
VISUAL_FLEET = "attr(tags, visual, //...)"


@dataclass(frozen=True)
class Render:
    """One PNG identified by target and output name."""

    target: str
    asset: str


@dataclass
class Observation:
    """What repeated runs saw of a single render: which run produced which bytes.

    Keeping the invocations, not just a count of distinct digests, is what makes a finding
    actionable: a drifted render is worth looking at, and looking at it means downloading
    both versions from the runs that produced them.
    """

    by_digest: dict[str, list[str]]

    @property
    def runs_present(self) -> int:
        return len({invocation for invocations in self.by_digest.values() for invocation in invocations})


@dataclass(frozen=True)
class Observations:
    review: dict[Render, Observation]
    diagnostics: dict[Render, Observation]


@dataclass(frozen=True)
class Execution:
    invocation: str
    command: tuple[str, ...]
    returncode: int


@dataclass(frozen=True)
class TargetObservations:
    """Separate observed target results from attempted runs with no BuildBuddy record."""

    runs: dict[str, list[TestRun]]
    invocations: list[str]
    unavailable_invocations: list[str]


def load_manifest(uri: str, *, bbapi: Path = Path("bbapi"), run: Runner = subprocess.run) -> VisualReviewManifest:
    result = run([bbapi, "--timeout", "30s", "blob", "cat", uri], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"bbapi blob cat failed for {uri}: {result.stderr.strip()}")
    return VisualReviewManifest.model_validate_json(result.stdout)


def visual_fleet(*, bbr: Path, run: Runner) -> list[str]:
    """Every browser target in the repo, asked of Bazel.

    Run from the workspace, like every `bbr` call here: `bbr` reads the git repository at its
    working directory, and under `bazel run` that is this binary's runfiles tree, which is not one.

    `streamed_jsonproto` rather than the default label output, because the runner's own progress
    shares this stdout: one target per line as JSON is a payload a log line cannot imitate, so a
    line either parses into a rule record or is not one. Reading labels as text would instead need
    a rule for telling them from log lines -- and the obvious one, a leading "//", is already wrong:
    the runner closes its last coloured line without a newline, so the first target arrives with an
    escape sequence glued to its front and no leading "//" at all.
    """
    result = run(
        [bbr, "query", "--output=streamed_jsonproto", VISUAL_FLEET],
        check=False,
        text=True,
        capture_output=True,
        cwd=get_build_workspace_directory(),
    )
    if result.returncode != 0:
        # Captured, so nothing `bbr` said has reached the log on its own: repeat it here, or the
        # failure reads as a bare exit status with no cause.
        raise SystemExit(f"`bbr query` exited {result.returncode}:\n{result.stderr}")
    labels = sorted({name for line in result.stdout.splitlines() if (name := _rule_label(line)) is not None})
    if not labels:
        raise SystemExit(f"`{VISUAL_FLEET}` matched nothing -- that tag is how the fleet is found")
    return labels


def _rule_label(line: str) -> str | None:
    """The label in one `streamed_jsonproto` line, or None if the line is not a rule record."""
    brace = line.find("{")
    if brace < 0:
        return None
    try:
        record = json.loads(line[brace:])
    except ValueError:
        return None
    rule = record.get("rule")
    return rule.get("name") if isinstance(rule, dict) and isinstance(rule.get("name"), str) else None


def run_once(targets: list[str], *, bbr: Path, run: Runner) -> Execution:
    """One full, uncached execution of `targets`, retaining its invocation and exit status.

    The ID is minted here and handed to the run rather than read back out of it: `bbr` honours
    an explicit `--invocation_id` (devinfra/buildbuddy/bbr.py), so the run's identity is known by
    construction, and what it did is then read from BuildBuddy instead of from its console.

    `--nocache_test_results` prevents Bazel from reusing a prior test result and marks its
    TestRunner spawn non-cacheable. Leave remote cache reads enabled so prerequisite build
    actions can still use them; `--noremote_accept_cached` would disable those hits too.

    Retain nonzero exits so build failures cannot disappear behind stable screenshots or
    a missing test summary. Finish the other runs before reporting the failures.
    """
    invocation = str(uuid.uuid4())
    command: list[str | Path] = [bbr, "test", f"--invocation_id={invocation}", "--nocache_test_results", *targets]
    result = run(command, check=False, text=True, cwd=get_build_workspace_directory())
    return Execution(invocation, tuple(str(part) for part in command), result.returncode)


def observe(
    invocations: list[str], *, bbapi: Path, run: Runner, read_manifest: Callable[[str], VisualReviewManifest]
) -> Observations:
    """An image counts as published only when that run's manifest declares it."""
    artifacts = list_ci_artifacts(invocations, bbapi=bbapi, run=run)
    manifests: dict[str, VisualReviewManifest] = {}
    declared: dict[tuple[str, str, TestResultIdentity | None], set[str]] = defaultdict(set)
    review: dict[Render, Observation] = {}
    for listed in artifacts:
        if listed.artifact.file.name != f"test.outputs/{MANIFEST_NAME}":
            continue
        uri = listed.artifact.file.uri
        if uri not in manifests:
            manifests[uri] = read_manifest(uri)
        for asset in manifests[uri].assets:
            # A manifest only registers outputs from the exact test result that wrote it. A
            # target can have several shards with the same output name, and invocation + label
            # alone would let one shard's manifest authorize another shard's image.
            declared[listed.invocation_id, listed.artifact.label, listed.artifact.test_result_identity].add(asset.path)
            # Keep declarations even when the PNG is absent from every run.
            review.setdefault(Render(listed.artifact.label, asset.path), Observation(by_digest={}))

    diagnostics: dict[Render, Observation] = {}
    for listed in artifacts:
        if not listed.artifact.file.name.startswith("test.outputs/") or not listed.artifact.file.name.endswith(".png"):
            continue
        render = Render(listed.artifact.label, listed.artifact.file.name.removeprefix("test.outputs/"))
        if render in review:
            if render.asset not in declared[listed.invocation_id, render.target, listed.artifact.test_result_identity]:
                continue
            observation = review[render]
        else:
            observation = diagnostics.setdefault(render, Observation(by_digest={}))
        produced_by = observation.by_digest.setdefault(listed.artifact.file.uri, [])
        # Shards/retries may list identical artifacts more than once in an invocation.
        if listed.invocation_id not in produced_by:
            produced_by.append(listed.invocation_id)
    return Observations(review=review, diagnostics=diagnostics)


def observe_targets(invocations: list[str], targets: list[str], *, bbapi: Path, run: Runner) -> TargetObservations:
    """Every observed invocation's view of each target, keyed by label.

    `targets` is what was swept, so a listing that came back short can be completed rather than
    quietly reporting on fewer targets than ran. An invocation BuildBuddy does not know about is
    returned separately so it cannot hide other observations or count as a missing screenshot.
    """
    by_label: dict[str, list[TestRun]] = defaultdict(list)
    available_invocations: list[str] = []
    unavailable_invocations: list[str] = []
    for invocation in invocations:
        test_runs = list_test_runs(invocation, targets, bbapi=bbapi, run=run)
        if test_runs is None:
            unavailable_invocations.append(invocation)
            continue
        available_invocations.append(invocation)
        for test_run in test_runs:
            by_label[test_run.label].append(test_run)
    return TargetObservations(
        runs=dict(by_label), invocations=available_invocations, unavailable_invocations=unavailable_invocations
    )


@dataclass(frozen=True)
class Findings:
    """Images whose bytes differ or whose output is missing from some runs."""

    drifted: list[Render]
    missing: list[Render]


def analyze(observations: dict[Render, Observation], *, runs: int) -> Findings:
    """Images whose bytes differ between runs, or that some runs did not produce."""
    drifted = sorted(
        (render for render, seen in observations.items() if len(seen.by_digest) > 1),
        key=lambda render: (render.target, render.asset),
    )
    # A render that only some runs published is its own failure: the scene is not reliably
    # produced at all, which a digest comparison over the runs that did produce it hides.
    missing = sorted(
        (render for render, seen in observations.items() if seen.runs_present < runs),
        key=lambda render: (render.target, render.asset),
    )
    return Findings(drifted=drifted, missing=missing)


def _image_report(observations: dict[Render, Observation], *, runs: int) -> list[str]:
    if runs < 2:
        insufficient_lines = [f"Insufficient observed runs: {runs}; at least 2 are required to assess reproducibility."]
        if not observations:
            insufficient_lines.append("No images in this group.")
        else:
            insufficient_lines += [
                f"- `{render.target}` — `{render.asset}`: observed in {seen.runs_present}/{runs} invocation(s)"
                for render, seen in sorted(observations.items(), key=lambda item: (item[0].target, item[0].asset))
            ]
        return [*insufficient_lines, ""]
    if not observations:
        return ["No images in this group.", ""]
    findings = analyze(observations, runs=runs)
    lines: list[str] = []
    if not findings.drifted and not findings.missing:
        lines.append(f"All {len(observations)} image(s) reproduced identically in every run.")
    if findings.drifted:
        lines += [f"### {len(findings.drifted)} image(s) not reproducible", ""]
        for render in findings.drifted:
            lines.append(f"- `{render.target}` — `{render.asset}`")
            # Which run produced which bytes: the reader downloads one of each and diffs.
            lines += [
                f"  - {len(produced_by)}/{runs} run(s), first `{produced_by[0]}`"
                for produced_by in sorted(observations[render].by_digest.values(), key=len, reverse=True)
            ]
        lines.append("")
    if findings.missing:
        lines += [f"### {len(findings.missing)} image(s) absent from some runs", ""]
        lines += [
            f"- `{render.target}` — `{render.asset}`: present in {observations[render].runs_present}/{runs}"
            for render in findings.missing
        ]
        lines.append("")

    return lines


def report(
    observations: Observations,
    invocations: list[str],
    targets: dict[str, list[TestRun]],
    *,
    executions: Sequence[Execution] = (),
    unavailable_invocations: Sequence[str] = (),
) -> str:
    """Review-asset reproducibility, non-failing diagnostics, and execution health."""
    runs = len(invocations)
    lines = [f"# Visual determinism over {runs} observed runs", ""]
    if executions:
        lines += [f"{len(executions)} commands were attempted; {runs} produced BuildBuddy invocations.", ""]
    lines += ["Artifacts are retained in each invocation's undeclared test outputs:", ""]
    lines += [f"{index + 1}. `{invocation}`" for index, invocation in enumerate(invocations)]
    lines += ["", "```bash", "bbapi artifact download <invocation> '*.png' --all", "```", ""]
    lines += [
        "## Review assets",
        "",
        "Images declared in `visual-review.json`; differences or missing images fail the check.",
        "",
    ]
    lines += _image_report(observations.review, runs=runs)
    lines += [
        "",
        "## Diagnostic PNGs (informational)",
        "",
        "Unpublished PNGs do not affect the check's exit status.",
        "",
    ]
    lines += _image_report(observations.diagnostics, runs=runs)
    unavailable = set(unavailable_invocations)
    failed_executions = [
        execution for execution in executions if execution.returncode != 0 or execution.invocation in unavailable
    ]
    if failed_executions:
        lines += ["", "## Failed executions", ""]
        for execution in failed_executions:
            if execution.invocation in unavailable:
                outcome = "BuildBuddy has no invocation record"
            else:
                outcome = "BuildBuddy recorded the invocation"
            lines.append(
                f"- `{execution.invocation}`: {outcome}; bbr exited {execution.returncode}. "
                f"Command: `{shlex.join(execution.command)}`"
            )
    failed_targets = sorted(label for label, tests in targets.items() if any(test.status != "PASSED" for test in tests))
    if failed_targets:
        lines += ["", "## Non-passing test targets", ""]
        lines += [f"- `{label}`" for label in failed_targets]
    if not targets:
        lines += ["", "No test results found; the check fails.", ""]

    # Wall time per run, not summed over shards: it is what one shard had to finish inside, so
    # it is the number `size` is set from. A status other than PASSED is worth seeing beside it.
    lines += ["## Durations", "", "| target | shards | min | max | statuses |", "|---|---|---|---|---|"]
    for label, runs_of_target in sorted(targets.items()):
        seconds = [test_run.summary.wall_time.total_seconds() for test_run in runs_of_target]
        shards = sorted({test_run.summary.shard_count for test_run in runs_of_target})
        statuses = sorted({test_run.status for test_run in runs_of_target})
        lines.append(
            f"| `{label}` | {', '.join(str(count) for count in shards)} "
            f"| {min(seconds):.1f}s | {max(seconds):.1f}s | {', '.join(statuses)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "targets", nargs="*", help="Bazel target patterns to run repeatedly; default is every target tagged `visual`."
    )
    parser.add_argument("--runs", type=int, default=5, help="How many uncached executions (default 5).")
    parser.add_argument("--bbr", type=Path, default=Path("bbr"))
    parser.add_argument("--bbapi", type=Path, default=Path("bbapi"))
    parser.add_argument("--summary", type=Path, help="Write the markdown report here as well as to stdout.")
    args = parser.parse_args()
    # Before the query: resolving the fleet is a remote Bazel round trip, and spending it only to
    # reject an argument makes a typo cost a minute and look like a tool failure.
    if args.runs < 2:
        raise SystemExit("--runs must be at least 2; a single run cannot show reproducibility")

    targets = args.targets or visual_fleet(bbr=args.bbr, run=subprocess.run)

    executions = []
    for index in range(args.runs):
        print(f"run {index + 1}/{args.runs}: {' '.join(targets)}", flush=True)
        executions.append(run_once(targets, bbr=args.bbr, run=subprocess.run))

    attempted_invocations = [execution.invocation for execution in executions]
    target_observations = observe_targets(attempted_invocations, targets, bbapi=args.bbapi, run=subprocess.run)
    invocations = target_observations.invocations
    observations = observe(
        invocations,
        bbapi=args.bbapi,
        run=subprocess.run,
        read_manifest=lambda uri: load_manifest(uri, bbapi=args.bbapi, run=subprocess.run),
    )
    summary = report(
        observations,
        invocations,
        target_observations.runs,
        executions=executions,
        unavailable_invocations=target_observations.unavailable_invocations,
    )
    print(summary)
    if args.summary:
        args.summary.write_text(summary)

    findings = analyze(observations.review, runs=len(invocations))
    if (
        findings.drifted
        or findings.missing
        or len(invocations) < 2
        or any(execution.returncode != 0 for execution in executions)
        or target_observations.unavailable_invocations
        or not target_observations.runs
        or any(test.status != "PASSED" for tests in target_observations.runs.values() for test in tests)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
