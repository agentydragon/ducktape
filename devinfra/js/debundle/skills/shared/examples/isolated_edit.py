"""One reviewed module merge in a worker's input snapshot.

The adapter supplies the JSON stdout of ``snapshot create`` (or ``snapshot
verify`` after relocation), the chunk's configured module subdirectory, reviewed
paths relative to that subtree, and a new evidence directory. ``modules merge``
edits that subtree while assessing it against the complete transform context.
Run this file with ``--help`` for its standalone arguments. It uses the copied
engine and exported context to stage analysis, previews before applying through
the same effect verdict, then explicitly regenerates analysis.
The launcher saves argv/cwd in *.started.json before execution; CLI invocation
JSON and *.result.json separately record the interpreted request and exit
status, with stdout/stderr alongside them. Adapter smoke tests remain separate.
"""

import argparse
import json
import subprocess
from pathlib import Path
from typing import TypedDict, cast


class Snapshot(TypedDict):
    directory: str
    engine: str
    graph: str
    spec_root: str
    graph_source_root: str
    graph_package_roots: dict[str, str]
    source_root: str
    modules: str
    analysis_input_args: list[str]
    transform_context_args: list[str]
    regeneration_argv: list[str]


def recorded_run(argv: list[str], cwd: Path, evidence: Path) -> str:
    """Capture an attempted invocation and its actual result, including failures."""
    command = [argv[0], "--invocation-out", str(evidence.with_suffix(".invocation.json")), *argv[1:]]
    evidence.with_suffix(".started.json").write_text(json.dumps({"argv": command, "cwd": str(cwd)}) + "\n")
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    evidence.with_suffix(".stdout").write_text(result.stdout)
    evidence.with_suffix(".stderr").write_text(result.stderr)
    evidence.with_suffix(".result.json").write_text(json.dumps({"exit_code": result.returncode}) + "\n")
    if result.returncode:
        raise RuntimeError(
            f"command exited {result.returncode}; see {evidence.with_suffix('.stderr')}\n{result.stderr}"
        )
    return result.stdout


def graph_command(snapshot: Snapshot) -> list[str]:
    """Global relocation options, including every package root returned by the CLI."""
    argv = [
        snapshot["engine"],
        "--spec-root",
        snapshot["spec_root"],
        "--graph-source-root",
        snapshot["graph_source_root"],
    ]
    for name, root in snapshot["graph_package_roots"].items():
        argv.extend(["--graph-package-root", f"{name}={root}"])
    return argv


def merge_and_regenerate(
    snapshot: Snapshot, module_subdir: Path, sources: list[str], target: str, evidence: Path
) -> None:
    """Apply one reviewed merge; another edit needs the newly generated graph."""
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    cwd = Path(snapshot["directory"])
    verified = recorded_run([snapshot["engine"], "snapshot", "verify", str(cwd)], cwd, evidence / "baseline")
    snapshot = cast(Snapshot, json.loads(verified))
    modules = (Path(snapshot["modules"]) / module_subdir).resolve()
    modules.relative_to(Path(snapshot["modules"]).resolve())
    projection = cwd / "analysis/edit.projection"
    analysis = cwd / "analysis/edit.dba"
    recorded_run(
        [snapshot["engine"], "spec", "project-analysis", *snapshot["transform_context_args"], "--out", str(projection)],
        cwd,
        evidence / "project",
    )
    recorded_run(
        [
            snapshot["engine"],
            "analyze",
            "--projection",
            str(projection),
            *snapshot["analysis_input_args"],
            "--out",
            str(analysis),
        ],
        cwd,
        evidence / "analyze",
    )
    merge = [
        *graph_command(snapshot),
        "modules",
        "merge",
        "--graph",
        snapshot["graph"],
        "--modules",
        str(modules),
        "--source-root",
        snapshot["source_root"],
        "--format",
        "json",
        "--target",
        target,
        *snapshot["transform_context_args"],
        "--analysis",
        str(analysis),
    ]
    # Global capture options precede the command; source paths follow `--`.
    plan = [merge[0], "--print-invocation", *merge[1:], "--", *sources]
    (evidence / "apply.plan.json").write_text(subprocess.check_output(plan, cwd=cwd, text=True))
    recorded_run([*merge, "--dry-run", "--", *sources], cwd, evidence / "preview")
    recorded_run([*merge, "--", *sources], cwd, evidence / "apply")
    # The starting snapshot/graph now describes the pre-edit tree. Do not treat
    # `snapshot verify` on that edited tree as a post-edit acceptance check.
    recorded_run(snapshot["regeneration_argv"], cwd, evidence / "regenerate")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-report", type=Path, required=True)
    parser.add_argument("--module-subdir", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("sources", nargs="+")
    args = parser.parse_args()
    snapshot = cast(Snapshot, json.loads(args.snapshot_report.read_text()))
    merge_and_regenerate(snapshot, args.module_subdir, args.sources, args.target, args.evidence)


if __name__ == "__main__":
    main()
