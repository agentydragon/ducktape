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
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from devinfra.pr_visuals.artifacts import Runner, list_ci_artifacts

# bbr's closing line, the only place it reports which invocation the run became.
INVOCATION_LINE = re.compile(r"^bbr: invocation ([0-9a-f-]{36})", re.MULTILINE)
# Bazel's per-target result line: "//pkg:target    PASSED in 12.3s", with ANSI in between.
RESULT_LINE = re.compile(r"^(//[^\s]+?)\s+.*?(PASSED|FAILED|TIMEOUT|FLAKY).*?in ([\d.]+)s", re.MULTILINE)

# What a rendered scene is published as; anything else in the outputs is not a render.
RENDER_SUFFIX = ".png"


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


@dataclass(frozen=True)
class Run:
    """What one execution tells us: where its artifacts are, and what each target cost."""

    invocation: str
    durations: dict[str, float]


def run_once(targets: list[str], *, bbr: Path, run: Runner) -> Run:
    """One full, uncached execution of `targets`.

    Both cache flags are needed and neither is redundant: without `--nocache_test_results`
    Bazel replays the previous result, and without `--noremote_accept_cached` it takes a
    peer's. Either way a later run would observe the first run's bytes and every scene
    would look perfectly reproducible.
    """
    result = run(
        [bbr, "test", "--nocache_test_results", "--noremote_accept_cached", *targets],
        check=False,
        text=True,
        capture_output=True,
    )
    combined = result.stdout + result.stderr
    found = INVOCATION_LINE.search(combined)
    if found is None:
        raise RuntimeError(f"no invocation id in bbr output (exit {result.returncode}):\n{combined[-2000:]}")
    return Run(found.group(1), durations(combined))


def durations(output: str) -> dict[str, float]:
    """Per-target wall time, for sizing. A target appears once per run."""
    return {target: float(seconds) for target, _status, seconds in RESULT_LINE.findall(output)}


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


def report(observations: dict[Render, Observation], invocations: list[str], timings: dict[str, list[float]]) -> str:
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

    lines += ["## Durations", "", "| target | min | max |", "|---|---|---|"]
    lines += [
        f"| `{target}` | {min(seconds):.1f}s | {max(seconds):.1f}s |" for target, seconds in sorted(timings.items())
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="Bazel target patterns to run repeatedly.")
    parser.add_argument("--runs", type=int, default=5, help="How many uncached executions (default 5).")
    parser.add_argument("--bbr", type=Path, default=Path("bbr"))
    parser.add_argument("--bbapi", type=Path, default=Path("bbapi"))
    parser.add_argument("--summary", type=Path, help="Write the markdown report here as well as to stdout.")
    args = parser.parse_args()

    if args.runs < 2:
        raise SystemExit("--runs must be at least 2; a single run cannot show reproducibility")

    invocations: list[str] = []
    timings: dict[str, list[float]] = defaultdict(list)
    for index in range(args.runs):
        print(f"run {index + 1}/{args.runs}: {' '.join(args.targets)}", flush=True)
        executed = run_once(args.targets, bbr=args.bbr, run=subprocess.run)
        invocations.append(executed.invocation)
        for target, seconds in executed.durations.items():
            timings[target].append(seconds)

    observations = observe(invocations, bbapi=args.bbapi, run=subprocess.run)
    if not observations:
        raise SystemExit(f"no renders published by {args.targets}; nothing to compare")

    summary = report(observations, invocations, dict(timings))
    print(summary)
    if args.summary:
        args.summary.write_text(summary)

    # Exit code is the signal a scheduled run reports on; the markdown says which renders.
    if any(len(seen.by_digest) > 1 or seen.runs_present < args.runs for seen in observations.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
