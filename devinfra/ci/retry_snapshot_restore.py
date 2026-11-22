"""Retry a current PR's first CI attempt after a specific pre-execution Firecracker failure."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from devinfra.buildbuddy import cli
from devinfra.buildbuddy.ci import bes
from devinfra.buildbuddy.ci.invocation_ids import invocation_id

# BuildBuddy truncates the execution status after "within the snapshot".
RESTORE_PREFIX = "start machine: failed to load a snapshot for VM: [PUT /snapshot/load][400] loadSnapshotBadRequest"
TSC_ERROR = "Failed to build microVM from snapshot: Could not set TSC scaling within the snapshot"


def restore_failure(invocation: bes.Invocation, executions: list[dict[str, Any]]) -> bool:
    if not (
        invocation.finished
        and not invocation.successful
        and invocation.command == "test"
        and invocation.exit_code_name == "REMOTE_ERROR"
        and all(summary.status == "PASSED" for summary in invocation.test_summaries)
    ):
        return False
    failed = False
    for execution in executions:
        if execution["stage"] != "COMPLETED":
            return False
        status = execution["status"]
        if status.get("code", 0) == 0 and execution.get("exitCode", 0) == 0:
            continue
        if not (
            execution["actionMnemonic"] == "TestRunner"
            and execution.get("exitCode") == -2
            and status.get("code") == 14
            and status["message"].startswith(RESTORE_PREFIX)
            and TSC_ERROR in status["message"]
        ):
            return False
        failed = True
    return failed


def failed_job(record: dict[str, Any]) -> int | None:
    jobs = record["jobs"]
    if len(jobs) != record["total_count"]:
        return None
    failed = [job for job in jobs if job["conclusion"] not in ("success", "skipped", "neutral")]
    if len(failed) != 1:
        return None
    job = failed[0]
    steps = [step["name"] for step in job["steps"] if step["conclusion"] not in ("success", "skipped", "neutral")]
    if job["name"] == "bazel-ci / Test & Build" and job["conclusion"] == "failure" and steps == ["Test + Build + lint"]:
        return int(job["id"])
    return None


def get(path: str) -> Any:
    return json.loads(subprocess.check_output(["gh", "api", path]))


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    run = event["workflow_run"]
    repo = event["repository"]
    prefix = f"repos/{repo['full_name']}"
    run_path = f"{prefix}/actions/runs/{int(run['id'])}"
    job = failed_job(get(f"{run_path}/attempts/1/jobs?per_page=100"))
    if job is None:
        print("No isolated Bazel CI step failure.")
        return
    test_id = str(invocation_id(run_id=str(run["id"]), attempt="1", role="test"))
    invocation = bes.read(test_id)
    if not restore_failure(invocation, json.loads(cli.run("execution", test_id, "--json"))["execution"]):
        print(f"Test invocation {test_id} did not qualify for retry.")
        return

    # Completed workflow events can have an empty pull_requests array. Resolve the
    # current open PR from its same-repository head branch and confirm the exact SHA.
    query = urlencode({"state": "open", "head": f"{repo['owner']['login']}:{run['head_branch']}"})
    pulls = get(f"{prefix}/pulls?{query}")
    if len(pulls) != 1 or pulls[0]["head"]["repo"]["id"] != repo["id"] or pulls[0]["head"]["sha"] != run["head_sha"]:
        print("The CI head is no longer the head of one open PR.")
        return
    latest = get(run_path)
    if latest["run_attempt"] != 1 or latest["status"] != "completed" or latest["conclusion"] != "failure":
        print("The original failed attempt has already been superseded.")
        return
    print(f"Retrying job {job} after BuildBuddy snapshot restore failed in {test_id}.", flush=True)
    subprocess.run(["gh", "api", "--method", "POST", f"{prefix}/actions/jobs/{job}/rerun"], check=True)


if __name__ == "__main__":
    main()
