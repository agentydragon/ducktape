"""Find and apply one checked residual peel in a verified worker snapshot.

The adapter supplies snapshot-create/verify JSON, the chunk's module subdirectory
and an evidence directory. This recipe uses the shipped isolated_edit helpers.
Review the saved plan and synthesized selector preview before adapting this
example to an authoring task. Graph acceptance and materialization are recorded
separately; application never replaces the adapter's behavioral checks.
"""

import argparse
import json
from pathlib import Path
from typing import cast

from isolated_edit import Snapshot, graph_command, recorded_run


def peel_more(snapshot: Snapshot, module_subdir: Path, evidence: Path, max_checks: int) -> None:
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    cwd = Path(snapshot["directory"])
    snapshot = cast(
        Snapshot,
        json.loads(recorded_run([snapshot["engine"], "snapshot", "verify", str(cwd)], cwd, evidence / "baseline")),
    )
    modules = (Path(snapshot["modules"]) / module_subdir).resolve()
    modules.relative_to(Path(snapshot["modules"]).resolve())
    common = ["--graph", snapshot["graph"], "--modules", str(modules), "--source-root", snapshot["source_root"]]
    command = [*graph_command(snapshot), "modules"]
    report = json.loads(
        recorded_run(
            [
                *command,
                "propose",
                "--strategy",
                "complete",
                "--max-checks",
                str(max_checks),
                *common,
                "--format",
                "json",
            ],
            cwd,
            evidence / "propose",
        )
    )
    if report["status"] == "found":
        plan = evidence / "plan.json"
        plan.write_text(json.dumps(report["plan"], indent=2) + "\n")
        recorded_run([*command, "check", "--plan", str(plan), *common, "--format", "json"], cwd, evidence / "check")
        apply = [*command, "apply", "--plan", str(plan), *common, "--format", "json"]
        recorded_run([*apply, "--dry-run"], cwd, evidence / "preview")
        recorded_run(apply, cwd, evidence / "apply")
        recorded_run(snapshot["regeneration_argv"], cwd, evidence / "regenerate")
    elif report["status"] != "exhausted_no_peel":
        raise RuntimeError(f"no accepted witness and no exhaustive conclusion: {report['status']}")
    print(json.dumps(report))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-report", type=Path, required=True)
    parser.add_argument("--module-subdir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--max-checks", type=int, required=True, help="Explicit budget; exhaustion is incomplete and fails"
    )
    args = parser.parse_args()
    peel_more(
        cast(Snapshot, json.loads(args.snapshot_report.read_text())), args.module_subdir, args.evidence, args.max_checks
    )


if __name__ == "__main__":
    main()
