"""Find and apply one checked residual peel in a verified worker snapshot.

The adapter supplies snapshot-create/verify JSON and an evidence directory.
The copied tree config scopes the graph's chunk. This recipe uses the shipped
isolated_edit helpers.
Review the saved plan and synthesized selector preview before adapting this
example to an authoring task. Graph acceptance and materialization are recorded
separately; application never replaces the adapter's behavioral checks.
"""

import argparse
import json
from pathlib import Path
from typing import cast

from isolated_edit import Snapshot, graph_command, recorded_run


def peel_more(snapshot: Snapshot, evidence: Path) -> None:
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    cwd = Path(snapshot["directory"])
    snapshot = cast(
        Snapshot,
        json.loads(recorded_run([snapshot["engine"], "snapshot", "verify", str(cwd)], cwd, evidence / "baseline")),
    )
    common = [
        "--graph",
        snapshot["graph"],
        "--modules",
        snapshot["modules"],
        "--source-root",
        snapshot["source_root"],
        "--tree-config",
        snapshot["tree_config"],
    ]
    command = [*graph_command(snapshot), "modules"]
    report = json.loads(recorded_run([*command, "propose", *common, "--format", "json"], cwd, evidence / "propose"))
    if report["status"] == "found":
        plan = evidence / "plan.json"
        plan.write_text(json.dumps(report["plan"], indent=2) + "\n")
        recorded_run([*command, "check", "--plan", str(plan), *common, "--format", "json"], cwd, evidence / "check")
        apply = [*command, "apply", "--plan", str(plan), *common, "--format", "json"]
        recorded_run([*apply, "--dry-run"], cwd, evidence / "preview")
        recorded_run(apply, cwd, evidence / "apply")
        recorded_run(snapshot["regeneration_argv"], cwd, evidence / "regenerate")
    elif report["status"] != "exhausted_no_peel":
        raise RuntimeError(f"no executable plan or completed result in the reported policy: {report['status']}")
    print(json.dumps(report))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-report", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    peel_more(cast(Snapshot, json.loads(args.snapshot_report.read_text())), args.evidence)


if __name__ == "__main__":
    main()
