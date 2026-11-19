"""Select uncached managed VM results and archive both routes for the CI experiment."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from devinfra.buildbuddy.ci.invocation_ids import invocation_id
from devinfra.buildbuddy.cli import CLIError, run

_COMPLETE = "COMPLETE_INVOCATION_STATUS"


def _json(*args: str) -> dict[str, Any]:
    return json.loads(run("--json", *args, timeout=30))


def _target(inv_id: str, label: str) -> dict[str, Any]:
    data = _json("target", inv_id, "--label", label)
    return next(
        (t for g in data.get("targetGroups", []) for t in g.get("targets", []) if t["metadata"]["label"] == label), {}
    )


def first_attempt(target: dict[str, Any]) -> str | None:
    for event in target.get("testResultEvents", []):
        if event["id"]["testResult"]["attempt"] != 1:
            continue
        result = event["testResult"]
        info = result.get("executionInfo", {})
        if result.get("cachedLocally") or info.get("cachedRemotely") or info.get("strategy") == "remote cache hit":
            return "cached"
        if info.get("strategy") == "remote":
            return "fresh"
    return None


def _managed_job_finished() -> bool:
    endpoint = "repos/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}/attempts/{GITHUB_RUN_ATTEMPT}/jobs".format_map(
        os.environ
    )
    jobs = json.loads(subprocess.check_output(["gh", "api", endpoint + "?per_page=100"], timeout=30))
    return any(j["name"] == "bazel-ci / Test & Build" and j["status"] == "completed" for j in jobs["jobs"])


def _select(inv_id: str, label: str) -> str:
    deadline = time.monotonic() + 60 * 65
    while time.monotonic() < deadline:
        try:
            rows = _json("invocation", "status", inv_id).get("invocation", [])
            if rows:
                selection = first_attempt(_target(inv_id, label))
                if selection:
                    return selection
                if rows[0].get("invocationStatus") == _COMPLETE:
                    return "no-fresh-result"
            if _managed_job_finished():
                # Re-read after GitHub signals completion, including jobs that died before Bazel started.
                if not _json("invocation", "status", inv_id).get("invocation"):
                    return "no-invocation"
                return first_attempt(_target(inv_id, label)) or "no-fresh-result"
        except (CLIError, subprocess.SubprocessError) as error:
            print(f"Retrying managed result lookup: {error}", file=sys.stderr, flush=True)
        time.sleep(30)
    return "observation-timeout"


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _collect(path: Path, inv_id: str, label: str) -> None:
    path.mkdir(exist_ok=True)
    target = _target(inv_id, label)
    _write(path / "target.json", target)
    _write(
        path / "invocation.json",
        [
            {k: r.get(k) for k in ("invocationId", "commitSha", "invocationStatus", "success")}
            for r in _json("invocation", "status", inv_id).get("invocation", [])
        ],
    )
    # Files named by BES can expire from CAS. Keep the scenario verdicts and fixture reuse
    # evidence with the pair. Full console logs stay in BuildBuddy.
    executions = _json("execution", inv_id).get("execution", [])
    _write(
        path / "executions.json",
        [e for e in executions if e.get("targetLabel") == label and e.get("actionMnemonic") == "TestRunner"],
    )
    for event in target.get("testResultEvents", []):
        identity = event["id"]["testResult"]
        dest = path / "{run}-{shard}-{attempt}".format_map(identity)
        dest.mkdir(exist_ok=True)
        for output in event["testResult"].get("testActionOutput", []):
            if output["name"] == "test.xml":
                xml = ElementTree.fromstring(run("blob", "cat", output["uri"], "--invocation", inv_id))
                _write(
                    dest / "cases.json",
                    [
                        {
                            "name": case.get("name"),
                            "class": case.get("classname"),
                            "seconds": case.get("time"),
                            "problems": [
                                {"kind": p.tag, "message": p.get("message", "").partition("\n")[0]}
                                for p in case
                                if p.tag in {"failure", "error", "skipped"}
                            ],
                        }
                        for case in xml.iter("testcase")
                    ],
                )
            elif output["name"] == "test.outputs/test-spans.jsonl":
                spans = run("blob", "cat", output["uri"], "--invocation", inv_id)
                _write(
                    dest / "fixture.json",
                    [
                        attrs
                        for line in spans.splitlines()
                        if (
                            attrs := {
                                k: v for k, v in json.loads(line)["attributes"].items() if k.startswith("persistence.")
                            }
                        )
                    ],
                )


def main() -> None:
    directory = Path(os.environ["PAIR_DIR"])
    directory.mkdir(exist_ok=True)
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    managed = str(invocation_id(run_id=run_id, attempt=attempt, role="test"))
    shadow = str(invocation_id(run_id=run_id, attempt=attempt, role="vm-shadow"))
    managed_label, shadow_label = os.environ["MANAGED_LABEL"], os.environ["SHADOW_LABEL"]
    manifest = directory / "pair.json"
    if sys.argv[1] == "select":
        record = {
            "sha": os.environ["GITHUB_SHA"],
            "run_id": run_id,
            "run_attempt": attempt,
            "managed": managed,
            "shadow": shadow,
            "managed_label": managed_label,
            "shadow_label": shadow_label,
            "selection": "waiting",
        }
        _write(manifest, record)
        record["selection"] = _select(managed, managed_label)
        _write(manifest, record)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            print(f"selection={record['selection']}\nshadow_id={shadow}", file=output)
        return

    if sys.argv[1] != "collect":
        raise SystemExit("expected select or collect")
    record = json.loads(manifest.read_text())
    record["shadow_step"] = os.environ["SHADOW_OUTCOME"]
    _write(manifest, record)
    routes = [("managed", managed, managed_label)]
    if record["shadow_step"] != "skipped":
        routes.append(("shadow", shadow, shadow_label))
    failures = []
    for route, inv_id, label in routes:
        try:
            if route == "managed" and record["selection"] == "fresh":
                # KVM may finish while the managed retry is still running.
                for _ in range(60):
                    rows = _json("invocation", "status", inv_id).get("invocation", [])
                    if rows and rows[0].get("invocationStatus") == _COMPLETE:
                        break
                    time.sleep(15)
                else:
                    failures.append("managed: invocation still incomplete at archive deadline")
            _collect(directory / route, inv_id, label)
        except (CLIError, ValueError, ElementTree.ParseError) as error:
            failures.append(f"{route}: {error}")
    if failures:
        _write(directory / "collection-errors.json", failures)
        raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
