"""Run visual targets repeatedly and compare collected screenshot outputs.

The comparison uses framework-neutral output records, exact BES result identity,
and BuildBuddy's content-addressed payload digests. Only complete successful
target run rosters contribute to missing-image checks; unavailable, failed, or
incomplete runs are reported on their own and never count as missing images.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from devinfra.pr_visuals.artifacts import Runner
from devinfra.pr_visuals.collection import CollectedOutputs, OutputObservation, ResultCoverage, collect_outputs
from devinfra.pr_visuals.targets import TestRun, list_test_runs
from util.bazel.workspace import get_build_workspace_directory

# The BuildBuddy collection scope is explicit and shared with the visual publisher.
REPOSITORY = "agentydragon/ducktape"

# Every target that drives a browser carries this tag -- `visual_test` applies it, and the
# screenshot macros set it directly -- so the fleet is a question for Bazel rather than a list
# somebody has to remember to update. It has already gone stale once: four per-scenario airlock
# targets outlived their collapse into one sweep.
VISUAL_FLEET = "attr(tags, visual, //...)"


@dataclass(frozen=True)
class Render:
    """A screenshot's protocol identity within the target collection scope."""

    target: str
    case_id: str
    checkpoint: str | None
    role: str


@dataclass(frozen=True)
class Roster:
    """One selected test run, assembled from its complete set of successful shards."""

    target: str
    invocation: str
    configuration: str
    run: int

    @property
    def display(self) -> str:
        return f"`{self.invocation}` (run {self.run})"


@dataclass
class Observation:
    """Which successful complete run roster produced each screenshot digest."""

    by_digest: dict[str, list[str]] = field(default_factory=dict)
    rosters: set[Roster] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)

    @property
    def runs_present(self) -> int:
        return len(self.rosters)


@dataclass(frozen=True)
class Observations:
    screenshots: dict[Render, Observation]
    diagnostics: tuple[OutputObservation, ...]
    complete_rosters: dict[str, tuple[Roster, ...]]
    incomplete_rosters: tuple[str, ...]
    issues: tuple[str, ...]
    collection_complete: bool


@dataclass(frozen=True)
class Execution:
    invocation: str
    command: tuple[str, ...]
    returncode: int


@dataclass(frozen=True)
class TargetObservations:
    """Per-invocation test results and attempts without a BuildBuddy invocation record."""

    runs: dict[str, list[TestRun]]
    runs_by_invocation: dict[str, dict[str, list[TestRun]]]
    invocations: list[str]
    unavailable_invocations: list[str]


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


def resolve_test_targets(patterns: list[str], *, bbr: Path, run: Runner) -> list[str]:
    """Resolve user-supplied Bazel patterns to concrete test labels before collection.

    The collector's `targets=` argument is a set of canonical test labels, not Bazel query
    expressions or patterns. Resolve each input independently so an unmatched pattern cannot be
    silently hidden by another pattern that did match.
    """
    labels: set[str] = set()
    for pattern in patterns:
        expression = f'kind(".*_test rule", {pattern})'
        result = run(
            [bbr, "query", "--output=streamed_jsonproto", expression],
            check=False,
            text=True,
            capture_output=True,
            cwd=get_build_workspace_directory(),
        )
        if result.returncode != 0:
            raise SystemExit(f"`bbr query` for `{pattern}` exited {result.returncode}:\n{result.stderr}")
        matches = {name for line in result.stdout.splitlines() if (name := _rule_label(line)) is not None}
        if not matches:
            raise SystemExit(f"Bazel test pattern `{pattern}` matched no test targets")
        labels.update(matches)
    return sorted(labels)


def run_once(targets: list[str], *, bbr: Path, run: Runner) -> Execution:
    """One uncached execution of `targets`, retaining its invocation and exit status."""
    invocation = str(uuid.uuid4())
    command: list[str | Path] = [bbr, "test", f"--invocation_id={invocation}", "--nocache_test_results", *targets]
    result = run(command, check=False, text=True, cwd=get_build_workspace_directory())
    return Execution(invocation, tuple(str(part) for part in command), result.returncode)


def observe_targets(invocations: list[str], targets: list[str], *, bbapi: Path, run: Runner) -> TargetObservations:
    """Every observed invocation's view of each target, keyed by label.

    `targets` is what was swept, so a listing that came back short can be completed rather than
    quietly reporting on fewer targets than ran. An invocation BuildBuddy does not know about is
    returned separately so it cannot hide other observations or count as a missing screenshot.
    """
    by_label: dict[str, list[TestRun]] = defaultdict(list)
    by_invocation: dict[str, dict[str, list[TestRun]]] = {}
    available_invocations: list[str] = []
    unavailable_invocations: list[str] = []
    for invocation in invocations:
        test_runs = list_test_runs(invocation, targets, bbapi=bbapi, run=run)
        if test_runs is None:
            unavailable_invocations.append(invocation)
            continue
        available_invocations.append(invocation)
        invocation_runs: dict[str, list[TestRun]] = defaultdict(list)
        for test_run in test_runs:
            by_label[test_run.label].append(test_run)
            invocation_runs[test_run.label].append(test_run)
        by_invocation[invocation] = dict(invocation_runs)
    return TargetObservations(
        runs=dict(by_label),
        runs_by_invocation=by_invocation,
        invocations=available_invocations,
        unavailable_invocations=unavailable_invocations,
    )


def _render(output: OutputObservation) -> Render:
    return Render(output.key.target, output.key.case_id, output.key.checkpoint, output.key.role)


def _roster_id(result: ResultCoverage) -> tuple[str, str, str, int] | None:
    source = result.result
    if source.configuration is None or source.run is None:
        return None
    return source.invocation, source.target, source.configuration, source.run


def from_collection(
    collected: CollectedOutputs,
    invocations: list[str],
    targets: list[str],
    target_runs: dict[str, dict[str, list[TestRun]]],
) -> Observations:
    """Build comparable screenshot rosters from selected terminal result observations."""
    results_by_target_invocation: dict[tuple[str, str], list[ResultCoverage]] = defaultdict(list)
    result_attempts: dict[tuple[str, str, str, int, int], list[ResultCoverage]] = defaultdict(list)
    configurations: dict[str, set[str]] = defaultdict(set)
    incomplete: list[str] = []

    for result in collected.results:
        source = result.result
        results_by_target_invocation[source.invocation, source.target].append(result)
        if source.configuration is not None:
            configurations[source.target].add(source.configuration)
        if source.configuration is None or source.run is None or source.shard is None or source.attempt is None:
            incomplete.append(f"{source.invocation} / {source.target}: incomplete native test result identity")
            continue
        result_attempts[(source.invocation, source.target, source.configuration, source.run, source.shard)].append(
            result
        )

    # An opaque configuration hash is provenance, not a comparison profile. Until an explicit
    # compatible profile exists, only a single configuration per target is comparable.
    ambiguous_targets = {target for target, values in configurations.items() if len(values) > 1}
    incomplete.extend(
        f"{target}: multiple configurations have no declared comparison profile" for target in sorted(ambiguous_targets)
    )

    roster_shards: dict[tuple[str, str, str, int], list[ResultCoverage]] = defaultdict(list)
    for shard_key, attempts in result_attempts.items():
        terminal = max(attempts, key=lambda item: item.result.attempt or 0)
        invocation, target, configuration, run_number, _shard = shard_key
        roster_shards[invocation, target, configuration, run_number].append(terminal)

    successful_rosters: dict[str, list[Roster]] = defaultdict(list)
    unusable_rosters: set[tuple[str, str, str, int]] = set()
    expected_targets = set(targets)
    for invocation in invocations:
        for target in sorted(expected_targets):
            rows = results_by_target_invocation.get((invocation, target), [])
            if not rows:
                incomplete.append(f"{invocation} / {target}: no test result coverage; excluded from missing checks")
                continue
            target_rows = target_runs.get(invocation, {}).get(target, [])
            if not target_rows:
                incomplete.append(f"{invocation} / {target}: no target test summary; excluded from missing checks")
                continue
            if any(row.status == "FAILED" for row in target_rows):
                incomplete.append(f"{invocation} / {target}: test target failed; excluded from missing checks")

    for roster_id, shard_results in roster_shards.items():
        invocation, target, configuration, run_number = roster_id
        if target in ambiguous_targets:
            unusable_rosters.add(roster_id)
            continue
        target_rows = target_runs.get(invocation, {}).get(target, [])
        if not target_rows or any(row.status == "FAILED" for row in target_rows):
            unusable_rosters.add(roster_id)
            continue
        if not all(result.selected and not result.issues for result in shard_results):
            unusable_rosters.add(roster_id)
            incomplete.append(
                f"{invocation} / {target} / run {run_number}: incomplete or unsuccessful shard roster; "
                "excluded from missing checks"
            )
            continue
        roster = Roster(target, invocation, configuration, run_number)
        successful_rosters[target].append(roster)

    selected_by_roster: dict[tuple[str, str, str, int], list[OutputObservation]] = defaultdict(list)
    for output in collected.selected:
        result_key = output.result
        if result_key.configuration is None or result_key.run is None:
            continue
        roster_id = result_key.invocation, result_key.target, result_key.configuration, result_key.run
        if roster_id not in unusable_rosters:
            selected_by_roster[roster_id].append(output)

    screenshots: dict[Render, Observation] = {}
    diagnostics: list[OutputObservation] = [
        output for output in collected.observations if output.key.role != "screenshot"
    ]
    for target, rosters in successful_rosters.items():
        for roster in rosters[:]:
            roster_id = roster.invocation, roster.target, roster.configuration, roster.run
            outputs = selected_by_roster.get(roster_id, [])
            claims: dict[Render, list[OutputObservation]] = defaultdict(list)
            for output in outputs:
                if output.key.role == "screenshot":
                    claims[_render(output)].append(output)
            if not claims:
                rosters.remove(roster)
                unusable_rosters.add(roster_id)
                incomplete.append(
                    f"{roster.display} / {target}: completed visual target emitted no screenshot outputs; "
                    "excluded from missing checks"
                )
                continue
            duplicates = [render for render, claims_for_render in claims.items() if len(claims_for_render) > 1]
            invalid_digests = [
                render for render, claims_for_render in claims.items() if claims_for_render[0].digest is None
            ]
            if duplicates or invalid_digests:
                rosters.remove(roster)
                unusable_rosters.add(roster_id)
                reason = "duplicate screenshot identity across shards" if duplicates else "screenshot has no CAS digest"
                incomplete.append(f"{roster.display} / {target}: {reason}; excluded from missing checks")
                continue
            for render, claims_for_render in claims.items():
                output = claims_for_render[0]
                observation = screenshots.setdefault(render, Observation())
                observation.rosters.add(roster)
                observation.labels.add(output.label or "")
                assert output.digest is not None
                observation.by_digest.setdefault(output.digest, []).append(roster.display)

    # Sort and deduplicate report-only issues while preserving their readable source detail.
    return Observations(
        screenshots=screenshots,
        diagnostics=tuple(diagnostics),
        complete_rosters={
            target: tuple(sorted(rosters, key=lambda roster: (roster.invocation, roster.run, roster.configuration)))
            for target, rosters in successful_rosters.items()
        },
        incomplete_rosters=tuple(dict.fromkeys(incomplete)),
        issues=collected.issues,
        collection_complete=collected.complete,
    )


def observe(
    invocations: list[str],
    targets: list[str],
    target_runs: dict[str, dict[str, list[TestRun]]],
    *,
    bbapi: Path,
    run: Runner,
) -> Observations:
    """Collect output records for expected targets, preserving incomplete collection as an issue."""
    with tempfile.TemporaryDirectory(prefix="visual-determinism-") as output_directory:
        try:
            collected = collect_outputs(
                invocations, REPOSITORY, Path(output_directory), targets=targets, bbapi=bbapi, run=run
            )
        except Exception as exc:
            collected = CollectedOutputs((), (), (), (f"output collection failed: {exc}",), False)
    return from_collection(collected, invocations, targets, target_runs)


@dataclass(frozen=True)
class Findings:
    """Screenshots whose bytes differ or whose output is absent from a complete roster."""

    drifted: list[Render]
    missing: list[Render]


def _render_sort_key(render: Render) -> tuple[str, str, bool, str, str]:
    return render.target, render.case_id, render.checkpoint is not None, render.checkpoint or "", render.role


def analyze(observations: Observations) -> Findings:
    """Compare CAS digests across complete successful rosters for each target."""
    drifted = sorted(
        (render for render, seen in observations.screenshots.items() if len(seen.by_digest) > 1), key=_render_sort_key
    )
    missing = sorted(
        (
            render
            for render, seen in observations.screenshots.items()
            if any(roster not in seen.rosters for roster in observations.complete_rosters.get(render.target, ()))
        ),
        key=_render_sort_key,
    )
    return Findings(drifted=drifted, missing=missing)


def _identity(render: Render) -> str:
    checkpoint = "<default>" if render.checkpoint is None else render.checkpoint
    return f"`{render.target}` — case `{render.case_id}`, checkpoint `{checkpoint}`, role `{render.role}`"


def _image_report(observations: Observations) -> list[str]:
    if not observations.screenshots:
        return ["No screenshot records in the selected successful outputs.", ""]
    findings = analyze(observations)
    lines: list[str] = []
    if not findings.drifted and not findings.missing:
        stable = sum(len(value.by_digest) == 1 for value in observations.screenshots.values())
        lines.append(f"All {stable} observed screenshot identity(ies) have one CAS digest across complete rosters.")
    if findings.drifted:
        lines += [f"### {len(findings.drifted)} screenshot(s) have different CAS digests", ""]
        for render in findings.drifted:
            seen = observations.screenshots[render]
            lines.append(f"- {_identity(render)}")
            for digest, rosters in sorted(seen.by_digest.items()):
                lines.append(f"  - `{digest}`: {', '.join(rosters)}")
        lines.append("")
    if findings.missing:
        lines += [f"### {len(findings.missing)} screenshot(s) are absent from complete rosters", ""]
        for render in findings.missing:
            seen = observations.screenshots[render]
            roster_count = len(observations.complete_rosters.get(render.target, ()))
            lines.append(f"- {_identity(render)}: present in {seen.runs_present}/{roster_count} complete roster(s)")
        lines.append("")
    insufficient = sorted(target for target, rosters in observations.complete_rosters.items() if len(rosters) < 2)
    if insufficient:
        lines += ["### Insufficient complete repeated runs", ""]
        lines += [
            f"- `{target}`: {len(observations.complete_rosters[target])} complete roster(s); at least 2 are required."
            for target in insufficient
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
    """Screenshot reproducibility, non-screenshot outputs, and execution health."""
    lines = [f"# Visual determinism over {len(executions) or len(invocations)} attempted runs", ""]
    lines += [f"{len(invocations)} BuildBuddy invocation(s) were available for collection.", ""]
    if invocations:
        lines += ["Artifacts are retained in each invocation's undeclared test outputs:", ""]
        lines += [f"{index + 1}. `{invocation}`" for index, invocation in enumerate(invocations)]
        lines += ["", "```bash", "bbapi artifact download <invocation> 'test.outputs/test-outputs/**' --all", "```", ""]
    lines += ["## Screenshot determinism", "", "Only role `screenshot` is diffed, by the payload CAS digest.", ""]
    lines += _image_report(observations)

    if observations.diagnostics:
        roles = sorted({output.key.role for output in observations.diagnostics})
        lines += ["", "## Other output roles (informational)", ""]
        lines += [
            f"{len(observations.diagnostics)} non-screenshot output observation(s) were collected "
            f"for roles: {', '.join(f'`{role}`' for role in roles)}. These outputs are not diffed.",
            "",
        ]

    if observations.incomplete_rosters:
        lines += ["", "## Failed or incomplete target rosters", ""]
        lines += [f"- {issue}" for issue in observations.incomplete_rosters]
        lines.append("")
    if observations.issues:
        lines += ["", "## Collector issues", ""]
        lines += [f"- {issue}" for issue in observations.issues]
        lines.append("")
    if not observations.collection_complete:
        lines += ["", "Output collection was incomplete; the check fails.", ""]

    unavailable = set(unavailable_invocations)
    failed_executions = [
        execution for execution in executions if execution.returncode != 0 or execution.invocation in unavailable
    ]
    if failed_executions:
        lines += ["", "## Failed or unavailable invocations", ""]
        for execution in failed_executions:
            if execution.invocation in unavailable:
                outcome = "BuildBuddy has no invocation record"
            else:
                outcome = "BuildBuddy recorded the invocation"
            lines.append(
                f"- `{execution.invocation}`: {outcome}; bbr exited {execution.returncode}. "
                f"Command: `{shlex.join(execution.command)}`"
            )
        lines.append("")

    failed_targets = sorted(label for label, tests in targets.items() if any(test.status != "PASSED" for test in tests))
    if failed_targets:
        lines += ["", "## Non-passing test targets", ""]
        lines += [f"- `{label}`" for label in failed_targets]
        lines.append("")
    if not targets:
        lines += ["", "No test results found; the check fails.", ""]

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

    targets = (
        resolve_test_targets(args.targets, bbr=args.bbr, run=subprocess.run)
        if args.targets
        else visual_fleet(bbr=args.bbr, run=subprocess.run)
    )
    executions = []
    for index in range(args.runs):
        print(f"run {index + 1}/{args.runs}: {' '.join(targets)}", flush=True)
        executions.append(run_once(targets, bbr=args.bbr, run=subprocess.run))

    attempted_invocations = [execution.invocation for execution in executions]
    target_observations = observe_targets(attempted_invocations, targets, bbapi=args.bbapi, run=subprocess.run)
    invocations = target_observations.invocations
    observations = observe(
        invocations, targets, target_observations.runs_by_invocation, bbapi=args.bbapi, run=subprocess.run
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

    findings = analyze(observations)
    if (
        findings.drifted
        or findings.missing
        or any(len(rosters) < 2 for rosters in observations.complete_rosters.values())
        or observations.incomplete_rosters
        or not observations.collection_complete
        or len(invocations) < 2
        or any(execution.returncode != 0 for execution in executions)
        or target_observations.unavailable_invocations
        or not target_observations.runs
        or any(test.status != "PASSED" for tests in target_observations.runs.values() for test in tests)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
