"""Run visual targets repeatedly and report which renders are not reproducible.

A passing visual test proves the scene rendered, not that it renders the same pixels
twice. Left unchecked, drift is discovered as a misleading "X% changed" on somebody's
unrelated PR, weeks after the scene stopped being stable.

Two runs is the obvious check and is too few: a race that fires one time in five looks
perfectly stable across a pair, and the three instances this repo has already hit (an
unguarded Mantine animation, two mocked-fetch races) are exactly that shape. This runs N.

Nothing is downloaded: BuildBuddy's artifact URIs are content-addressed, so an asset that
rendered identically in every run reports one URI across all of them, and an asset that
drifted reports several. The same runs give per-target durations, which is the evidence
`size` should be set from -- see AGENTS.md on sizing from measurement rather than from a
timeout that once went red.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from devinfra.pr_visuals.artifacts import Runner, list_ci_artifacts
from devinfra.pr_visuals.targets import TestRun, list_test_runs

# What a rendered scene is published as; anything else in the outputs is not a render.
RENDER_SUFFIX = ".png"

# Every target that drives a browser carries this tag -- `visual_test` applies it, and the
# screenshot macros set it directly -- so the fleet is a question for Bazel rather than a list
# somebody has to remember to update. It has already gone stale once: four per-scenario airlock
# targets outlived their collapse into one sweep.
VISUAL_FLEET = "attr(tags, visual, //...)"


@dataclass(frozen=True)
class Render:
    """One published image, identified the way a reviewer would name it."""

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
        return sum(len(invocations) for invocations in self.by_digest.values())


def visual_fleet(*, bbr: Path, run: Runner) -> list[str]:
    """Every browser target in the repo, asked of Bazel.

    `streamed_jsonproto` rather than the default label output, because the runner's own progress
    shares this stdout: one target per line as JSON is a payload a log line cannot imitate, so a
    line either parses into a rule record or is not one. Reading labels as text would instead need
    a rule for telling them from log lines -- and the obvious one, a leading "//", is already wrong:
    the runner closes its last coloured line without a newline, so the first target arrives with an
    escape sequence glued to its front and no leading "//" at all.
    """
    result = run(
        [bbr, "query", "--output=streamed_jsonproto", VISUAL_FLEET], check=True, text=True, capture_output=True
    )
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


def run_once(targets: list[str], *, bbr: Path, run: Runner) -> str:
    """One full, uncached execution of `targets`, returning the invocation it became.

    The ID is minted here and handed to the run rather than read back out of it: `bbr` honours
    an explicit `--invocation_id` (devinfra/bbr.py), so the run's identity is known by
    construction, and what it did is then read from BuildBuddy instead of from its console.

    Both cache flags are needed and neither is redundant: without `--nocache_test_results`
    Bazel replays the previous result, and without `--noremote_accept_cached` it takes a
    peer's. Either way a later run would observe the first run's bytes and every scene
    would look perfectly reproducible.

    A failing target is not an error here -- its status reaches the report, where a reader can
    see it -- so only a `bbr` that could not run at all raises.
    """
    invocation = str(uuid.uuid4())
    run(
        [bbr, "test", f"--invocation_id={invocation}", "--nocache_test_results", "--noremote_accept_cached", *targets],
        check=False,
        text=True,
    )
    return invocation


def observe(invocations: list[str], *, bbapi: Path, run: Runner) -> dict[Render, Observation]:
    """Fold every run's published renders into one observation per render."""
    seen: dict[Render, Observation] = defaultdict(lambda: Observation(by_digest=defaultdict(list)))
    for listed in list_ci_artifacts(invocations, bbapi=bbapi, run=run):
        name = listed.artifact.name
        if not name.endswith(RENDER_SUFFIX):
            continue
        observation = seen[Render(listed.artifact.label, name.removeprefix("test.outputs/"))]
        observation.by_digest[listed.artifact.uri].append(listed.invocation_id)
    return dict(seen)


def observe_targets(invocations: list[str], *, bbapi: Path, run: Runner) -> dict[str, list[TestRun]]:
    """Every run's view of each target, keyed by label."""
    by_label: dict[str, list[TestRun]] = defaultdict(list)
    for invocation in invocations:
        for test_run in list_test_runs(invocation, bbapi=bbapi, run=run):
            by_label[test_run.label].append(test_run)
    return dict(by_label)


def report(observations: dict[Render, Observation], invocations: list[str], targets: dict[str, list[TestRun]]) -> str:
    """A markdown report: what drifted, what went missing, and what each target cost.

    Every render stays in BuildBuddy as the run's undeclared test outputs, so the report
    names the invocations rather than copying pixels anywhere: that is both where the PNGs
    already are and the only place a reader can get the two differing versions to compare.
    """
    runs = len(invocations)
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

    lines = [f"# Visual determinism over {runs} runs", ""]
    lines += ["Every run's renders are that invocation's undeclared test outputs:", ""]
    lines += [f"{index + 1}. `{invocation}`" for index, invocation in enumerate(invocations)]
    lines += ["", "```bash", "bbapi artifact download <invocation> '*.png' --all", "```", ""]

    if not drifted and not missing:
        lines.append(f"All {len(observations)} renders reproduced identically in every run.")
    if drifted:
        lines += [f"## {len(drifted)} render(s) not reproducible", ""]
        for render in drifted:
            lines.append(f"- `{render.target}` — `{render.asset}`")
            # Which run produced which bytes: the reader downloads one of each and diffs.
            lines += [
                f"  - {len(produced_by)}/{runs} run(s), first `{produced_by[0]}`"
                for produced_by in sorted(observations[render].by_digest.values(), key=len, reverse=True)
            ]
        lines.append("")
    if missing:
        lines += [f"## {len(missing)} render(s) not published by every run", ""]
        lines += [
            f"- `{render.target}` — `{render.asset}`: present in {observations[render].runs_present}/{runs}"
            for render in missing
        ]
        lines.append("")

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
    targets = args.targets or visual_fleet(bbr=args.bbr, run=subprocess.run)

    if args.runs < 2:
        raise SystemExit("--runs must be at least 2; a single run cannot show reproducibility")

    invocations = []
    for index in range(args.runs):
        print(f"run {index + 1}/{args.runs}: {' '.join(targets)}", flush=True)
        invocations.append(run_once(targets, bbr=args.bbr, run=subprocess.run))

    observations = observe(invocations, bbapi=args.bbapi, run=subprocess.run)
    if not observations:
        raise SystemExit(f"no renders published by {targets}; nothing to compare")

    summary = report(observations, invocations, observe_targets(invocations, bbapi=args.bbapi, run=subprocess.run))
    print(summary)
    if args.summary:
        args.summary.write_text(summary)

    # Exit code is the signal a scheduled run reports on; the markdown says which renders.
    if any(len(seen.by_digest) > 1 or seen.runs_present < args.runs for seen in observations.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
