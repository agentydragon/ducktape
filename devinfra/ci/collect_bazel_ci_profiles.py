"""Collect and summarize Bazel CI profile/probe data.

This is an investigation helper, not production CI code. It shells out to:

- `gh` for GitHub Actions metadata and logs;
- the repo-local `bbapi` binary for BuildBuddy build tool logs;
- `bb download` for probe bundles uploaded to BuildBuddy CAS.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import os
import re
import statistics
import subprocess
import tarfile
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CARET_ANSI_RE = re.compile(r"\^\[\[[0-9;]*m")
INVOCATION_RE = re.compile(r"Invocation ID: (?P<id>[0-9a-f-]{36})")
ANALYZING_RE = re.compile(
    r"Analyzing:\s*(?P<targets>\d+) targets "
    r"\((?P<packages>\d+) packages loaded, (?P<configured>\d+) targets configured\)?"
)
ELAPSED_RE = re.compile(r"Elapsed time: (?P<elapsed>[0-9.]+)s, Critical Path: (?P<critical>[0-9.]+)s")
PROCESS_RE = re.compile(r"(?P<count>\d+) process(?:es)?: (?P<text>.*)")
PROBE_SUMMARY_RE = re.compile(r"CI_VM_PROBE_SUMMARY (?P<kv>.*)")
PROBE_SERVER_RE = re.compile(r"CI_VM_PROBE_SERVER (?P<kv>.*)")
PROBE_ARCHIVE_RE = re.compile(r"CI_VM_PROBE_ARCHIVE (?P<kv>.*)")
PROBE_CAS_RE = re.compile(r"CI_VM_PROBE_CAS (?P<rest>.*)")


@dataclasses.dataclass
class CommandResult:
    argv: list[str]
    stdout: str
    stderr: str


def run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> CommandResult:
    proc = subprocess.run(argv, cwd=cwd, check=False, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed\nargv={argv!r}\nreturncode={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return CommandResult(argv=argv, stdout=proc.stdout, stderr=proc.stderr)


def intish(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def strip_ansi(line: str) -> str:
    return CARET_ANSI_RE.sub("", ANSI_RE.sub("", line))


def as_json_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def float_values(values: Iterable[Any]) -> list[float]:
    return [value for value in values if isinstance(value, float)]


def int_values(values: Iterable[Any]) -> list[int]:
    return [value for value in values if isinstance(value, int)]


def parse_kv(text: str) -> dict[str, str]:
    result = {}
    for part in text.split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key] = value
    return result


def parse_log(text: str) -> dict[str, Any]:
    invocations: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    probe_summaries = []
    probe_servers = []
    probe_archives = []
    probe_cas = []

    for raw in text.splitlines():
        line = strip_ansi(raw)
        if match := PROBE_SUMMARY_RE.search(line):
            probe_summaries.append(parse_kv(match.group("kv")))
            continue
        if match := PROBE_SERVER_RE.search(line):
            probe_servers.append(parse_kv(match.group("kv")))
            continue
        if match := PROBE_ARCHIVE_RE.search(line):
            probe_archives.append(parse_kv(match.group("kv")))
            continue
        if match := PROBE_CAS_RE.search(line):
            rest = match.group("rest")
            if rest.startswith("digest="):
                digest = rest.removeprefix("digest=")
                if digest:
                    probe_cas.append({"digest": digest})
                else:
                    probe_cas.append({"missing_digest": "empty"})
            elif rest.startswith("missing_digest="):
                probe_cas.append({"missing_digest": rest.removeprefix("missing_digest=")})
            else:
                probe_cas.append({"raw": rest})
            continue
        if match := INVOCATION_RE.search(line):
            role = "test" if not invocations else "build" if len(invocations) == 1 else f"command-{len(invocations)}"
            current = {"id": match.group("id"), "role": role, "analyzing": []}
            invocations.append(current)
            continue
        if current is None:
            continue
        if match := ANALYZING_RE.search(line):
            current["analyzing"].append(
                {
                    "targets": int(match.group("targets")),
                    "packages": int(match.group("packages")),
                    "configured": int(match.group("configured")),
                }
            )
            continue
        if match := ELAPSED_RE.search(line):
            current["elapsed_s"] = float(match.group("elapsed"))
            current["critical_path_s"] = float(match.group("critical"))
            continue
        if match := PROCESS_RE.search(line):
            current["process_count"] = int(match.group("count"))
            current["process_stats"] = match.group("text").strip()

    for invocation in invocations:
        analyzing = invocation.get("analyzing", [])
        if analyzing:
            invocation["max_packages_loaded"] = max(row["packages"] for row in analyzing)
            invocation["max_targets_configured"] = max(row["configured"] for row in analyzing)
            invocation["target_count"] = max(row["targets"] for row in analyzing)
    return {
        "invocations": invocations,
        "probe_summaries": probe_summaries,
        "probe_servers": probe_servers,
        "probe_archives": probe_archives,
        "probe_cas": probe_cas,
    }


def intervals_union_seconds(events: list[dict[str, Any]], category: str) -> float:
    intervals = []
    for event in events:
        if event.get("ph") == "X" and event.get("cat") == category and "dur" in event:
            start = float(event["ts"])
            intervals.append((start, start + float(event["dur"])))
    if not intervals:
        return 0.0
    intervals.sort()
    merged = []
    cur_start, cur_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return sum(end - start for start, end in merged) / 1_000_000.0


def profile_summary(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt") as f:
        data = json.load(f)
    events = data.get("traceEvents", data if isinstance(data, list) else [])

    markers = {event["name"]: event["ts"] for event in events if event.get("cat") == "build phase marker"}
    phase = {}
    if "Evaluate target patterns" in markers and "Load, analyze dependencies and build artifacts" in markers:
        phase["eval_to_load_marker_s"] = (
            markers["Load, analyze dependencies and build artifacts"] - markers["Evaluate target patterns"]
        ) / 1_000_000.0
    if "Load, analyze dependencies and build artifacts" in markers and "Complete build" in markers:
        phase["load_analyze_exec_span_s"] = (
            markers["Complete build"] - markers["Load, analyze dependencies and build artifacts"]
        ) / 1_000_000.0

    first_action_ts = min(
        (
            event["ts"]
            for event in events
            if event.get("ph") == "X" and event.get("cat") == "action processing" and "ts" in event
        ),
        default=None,
    )
    if first_action_ts is not None and "Load, analyze dependencies and build artifacts" in markers:
        phase["pre_action_after_load_marker_s"] = (
            first_action_ts - markers["Load, analyze dependencies and build artifacts"]
        ) / 1_000_000.0

    top_actions = []
    for event in events:
        if event.get("ph") != "X" or event.get("cat") != "action processing" or "dur" not in event:
            continue
        args = event.get("args", {})
        top_actions.append(
            {
                "duration_s": event["dur"] / 1_000_000.0,
                "name": event.get("name", ""),
                "target": args.get("target", ""),
                "mnemonic": args.get("mnemonic", ""),
            }
        )
    top_actions.sort(key=lambda row: row["duration_s"], reverse=True)

    critical_components = [
        {"duration_s": event["dur"] / 1_000_000.0, "name": event.get("name", "")}
        for event in events
        if event.get("ph") == "X" and event.get("cat") == "critical path component" and "dur" in event
    ]
    critical_components.sort(key=lambda row: row["duration_s"], reverse=True)

    return {
        "profile": str(path),
        "event_count": len(events),
        "phase": phase,
        "wall_by_category_s": {
            "action_processing": intervals_union_seconds(events, "action processing"),
            "remote_action_cache_check": intervals_union_seconds(events, "remote action cache check"),
            "remote_action_execution": intervals_union_seconds(events, "remote action execution"),
            "remote_execution_process_wall": intervals_union_seconds(events, "Remote execution process wall time"),
        },
        "top_actions": top_actions[:10],
        "critical_path_components": critical_components[:10],
    }


def fetch_bes_events(invocation_id: str) -> list[dict[str, Any]]:
    api_key = os.environ.get("BUILDBUDDY_API_KEY")
    if not api_key:
        raise RuntimeError("BUILDBUDDY_API_KEY environment variable is not set")
    base_url = os.environ.get("BUILDBUDDY_URL", "https://app.buildbuddy.io").rstrip("/")
    query = urllib.parse.urlencode({"invocation_id": invocation_id, "artifact": "raw_json"})
    req = urllib.request.Request(f"{base_url}/file/download?{query}", headers={"x-buildbuddy-api-key": api_key})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, list):
        raise RuntimeError(f"BES raw JSON for {invocation_id} was not a list")
    return cast(list[dict[str, Any]], data)


def bes_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    id_counts: dict[str, int] = {}
    for event in events:
        event_id = as_json_dict(event.get("id"))
        for key in event_id:
            id_counts[key] = id_counts.get(key, 0) + 1

    started: dict[str, Any] = next((as_json_dict(event.get("started")) for event in events if event.get("started")), {})
    metrics: dict[str, Any] = next(
        (as_json_dict(event.get("buildMetrics")) for event in reversed(events) if event.get("buildMetrics")), {}
    )
    action_summary = as_json_dict(metrics.get("actionSummary"))
    ac = as_json_dict(action_summary.get("actionCacheStatistics"))
    timing = as_json_dict(metrics.get("timingMetrics"))
    target = as_json_dict(metrics.get("targetMetrics"))
    package = as_json_dict(metrics.get("packageMetrics"))

    miss_details = {}
    for row_value in as_json_list(ac.get("missDetails")):
        row = as_json_dict(row_value)
        reason = row.get("reason", "UNKNOWN")
        miss_details[reason] = intish(row.get("count")) or 0

    runner_counts = {}
    for row_value in as_json_list(action_summary.get("runnerCount")):
        row = as_json_dict(row_value)
        name = row.get("name", "")
        if name:
            runner_counts[name] = intish(row.get("count")) or 0

    build_graph = as_json_dict(metrics.get("buildGraphMetrics"))
    skyframe = {}
    for field in ["dirtiedValues", "changedValues", "builtValues", "cleanedValues", "evaluatedValues"]:
        rows = {}
        for row_value in as_json_list(build_graph.get(field)):
            row = as_json_dict(row_value)
            name = row.get("skyfunctionName", "")
            if name:
                rows[name] = intish(row.get("count")) or 0
        if rows:
            skyframe[field] = rows

    def ms_to_s(key: str) -> float | None:
        value = intish(timing.get(key))
        if value is None:
            return None
        return value / 1000.0

    return {
        "event_counts": id_counts,
        "started": {
            "uuid": started.get("uuid"),
            "command": started.get("command"),
            "server_pid": intish(started.get("serverPid")),
            "host": started.get("host"),
            "start_time": started.get("startTime"),
            "build_tool_version": started.get("buildToolVersion"),
        },
        "build_metrics": {
            "packages_loaded": intish(package.get("packagesLoaded")) or 0,
            "targets_configured": intish(target.get("targetsConfigured")) or 0,
            "targets_configured_not_including_aspects": intish(target.get("targetsConfiguredNotIncludingAspects")) or 0,
            "analysis_phase_s": ms_to_s("analysisPhaseTimeInMs"),
            "execution_phase_s": ms_to_s("executionPhaseTimeInMs"),
            "wall_time_s": ms_to_s("wallTimeInMs"),
            "actions_execution_start_s": ms_to_s("actionsExecutionStartInMs"),
            "actions_created": intish(action_summary.get("actionsCreated")),
            "actions_executed": intish(action_summary.get("actionsExecuted")),
            "ac_hits": intish(ac.get("hits")),
            "ac_misses": intish(ac.get("misses")),
            "ac_miss_details": miss_details,
            "runner_counts": runner_counts,
            "skyframe": skyframe,
        },
    }


def gh_run_metadata(repo: str, run_id: str) -> dict[str, Any]:
    fields = "databaseId,workflowName,event,headBranch,headSha,displayTitle,status,conclusion,createdAt,updatedAt,jobs"
    data = json.loads(run(["gh", "run", "view", run_id, "--repo", repo, "--json", fields]).stdout)
    if not isinstance(data, dict):
        raise RuntimeError(f"GitHub run metadata for {run_id} was not a JSON object")
    return cast(dict[str, Any], data)


def bazel_job_id(run_meta: dict[str, Any]) -> str:
    for job in run_meta.get("jobs", []):
        if job.get("name") == "bazel-ci / Test & Build":
            return str(job["databaseId"])
    raise RuntimeError(f"no bazel-ci / Test & Build job in run {run_meta.get('databaseId')}")


def download_job_log(repo: str, run_id: str, job_id: str, out: Path) -> str:
    log = run(["gh", "run", "view", run_id, "--job", job_id, "--log", "--repo", repo]).stdout
    out.write_text(log)
    return log


def download_profile(bbapi: Path, invocation_id: str, out: Path) -> Path | None:
    out.parent.mkdir(parents=True, exist_ok=True)
    result = run([str(bbapi), "tool-log", "download", invocation_id, "command.profile.gz", "-o", str(out)], check=False)
    if "Downloaded to " not in result.stdout and not out.exists():
        return None
    return out if out.exists() else None


def decompress_profile(profile_gz: Path, profile_json: Path) -> None:
    with gzip.open(profile_gz, "rb") as src:
        profile_json.write_bytes(src.read())


def decompressed_profile_path(profile_gz: Path) -> Path:
    if profile_gz.name.endswith(".gz"):
        return profile_gz.with_name(profile_gz.name.removesuffix(".gz") + ".json")
    return profile_gz.with_suffix(profile_gz.suffix + ".json")


def download_probe_bundle(digest: str, out: Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    result = run(["bb", "download", digest, f"--output_file={out}"], check=False)
    return out.exists() and result.stderr == ""


def collect_run(repo: str, run_id: str, out_root: Path, bbapi: Path) -> dict[str, Any]:
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = gh_run_metadata(repo, run_id)
    job_id = bazel_job_id(meta)
    log_text = download_job_log(repo, run_id, job_id, run_dir / "github-job.log")
    parsed = parse_log(log_text)

    for invocation in parsed["invocations"]:
        try:
            events = fetch_bes_events(invocation["id"])
            invocation["bes_summary"] = bes_summary(events)
        except Exception as e:
            invocation["bes_error"] = repr(e)

        profile_gz = run_dir / "profiles" / f"{invocation['role']}-{invocation['id']}.command.profile.gz"
        downloaded = download_profile(bbapi, invocation["id"], profile_gz)
        if downloaded:
            profile_json = decompressed_profile_path(profile_gz)
            decompress_profile(profile_gz, profile_json)
            invocation["profile_gz"] = str(profile_gz)
            invocation["profile_json"] = str(profile_json)
            invocation["profile_summary"] = profile_summary(profile_gz)

    for i, cas in enumerate(parsed["probe_cas"]):
        digest = cas.get("digest")
        if not digest:
            continue
        probe_path = run_dir / "probes" / f"probe-{i}.tgz"
        if download_probe_bundle(digest, probe_path):
            cas["downloaded_to"] = str(probe_path)
            try:
                with tarfile.open(probe_path) as tar:
                    cas["tar_members"] = tar.getnames()
            except tarfile.TarError as e:
                cas["tar_error"] = repr(e)

    summary: dict[str, Any] = {"run": meta, "job_id": job_id, **parsed}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def fmt_s(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}"
    if value is None:
        return ""
    return str(value)


def median(values: list[float]) -> str:
    return f"{statistics.median(values):.2f}" if values else ""


def range_s(values: list[float]) -> str:
    return f"{min(values):.2f}-{max(values):.2f}" if values else ""


def invocation_by_role(summary: dict[str, Any], role: str) -> dict[str, Any]:
    for invocation_value in as_json_list(summary.get("invocations")):
        invocation = as_json_dict(invocation_value)
        if invocation.get("role") == role:
            return invocation
    return {}


def build_metrics(invocation: dict[str, Any]) -> dict[str, Any]:
    return as_json_dict(as_json_dict(invocation.get("bes_summary")).get("build_metrics"))


def write_aggregate(summaries: list[dict[str, Any]], out: Path) -> None:
    lines = ["# Bazel CI Profile Collection", ""]
    lines.append(
        md_row(
            [
                "run",
                "event",
                "head",
                "title",
                "test elapsed",
                "test critical",
                "BES pkg",
                "BES cfg",
                "BES analysis",
                "progress cfg",
                "build elapsed",
                "build BES cfg",
                "probe",
            ]
        )
    )
    lines.append(
        md_row(["---", "---", "---", "---", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---"])
    )
    for summary in summaries:
        test = invocation_by_role(summary, "test")
        build = invocation_by_role(summary, "build")
        test_bes = build_metrics(test)
        build_bes = build_metrics(build)
        probes = summary["probe_summaries"]
        probe = " / ".join(
            f"{p.get('phase')} prev={p.get('previous_run_log')} servers={p.get('bazel_servers')}" for p in probes
        )
        lines.append(
            md_row(
                [
                    str(summary["run"]["databaseId"]),
                    summary["run"]["event"],
                    summary["run"]["headSha"][:8],
                    summary["run"]["displayTitle"].replace("|", "\\|"),
                    fmt_s(test.get("elapsed_s")),
                    fmt_s(test.get("critical_path_s")),
                    fmt_s(test_bes.get("packages_loaded")),
                    fmt_s(test_bes.get("targets_configured")),
                    fmt_s(test_bes.get("analysis_phase_s")),
                    fmt_s(test.get("max_targets_configured")),
                    fmt_s(build.get("elapsed_s")),
                    fmt_s(build_bes.get("targets_configured")),
                    probe,
                ]
            )
        )

    test_elapsed = float_values(invocation_by_role(s, "test").get("elapsed_s") for s in summaries)
    test_critical = float_values(invocation_by_role(s, "test").get("critical_path_s") for s in summaries)
    test_cfg = int_values(build_metrics(invocation_by_role(s, "test")).get("targets_configured") for s in summaries)
    test_analysis = float_values(
        build_metrics(invocation_by_role(s, "test")).get("analysis_phase_s") for s in summaries
    )
    test_cfg_floats = [float(v) for v in test_cfg]

    lines.extend(
        [
            "",
            "## Aggregate Timings",
            md_row(["metric", "median", "range"]),
            md_row(["---", "---:", "---:"]),
            md_row(["test elapsed s", median(test_elapsed), range_s(test_elapsed)]),
            md_row(["test critical path s", median(test_critical), range_s(test_critical)]),
            md_row(["test BES analysis phase s", median(test_analysis), range_s(test_analysis)]),
            md_row(["test BES configured targets", median(test_cfg_floats), range_s(test_cfg_floats)]),
        ]
    )

    lines.extend(["", "## BES Build Metrics"])
    lines.append(
        md_row(
            [
                "run",
                "role",
                "pkg",
                "cfg",
                "analysis s",
                "execution s",
                "actions",
                "AC hits",
                "AC misses",
                "runner counts",
            ]
        )
    )
    lines.append(md_row(["---", "---", "---:", "---:", "---:", "---:", "---:", "---:", "---:", "---"]))
    for summary in summaries:
        for role in ["test", "build"]:
            invocation = invocation_by_role(summary, role)
            bes = build_metrics(invocation)
            if not bes:
                continue
            runner_counts = ", ".join(f"{k}={v}" for k, v in bes.get("runner_counts", {}).items())
            lines.append(
                md_row(
                    [
                        str(summary["run"]["databaseId"]),
                        role,
                        fmt_s(bes.get("packages_loaded")),
                        fmt_s(bes.get("targets_configured")),
                        fmt_s(bes.get("analysis_phase_s")),
                        fmt_s(bes.get("execution_phase_s")),
                        fmt_s(bes.get("actions_executed")),
                        fmt_s(bes.get("ac_hits")),
                        fmt_s(bes.get("ac_misses")),
                        runner_counts,
                    ]
                )
            )

    lines.extend(["", "## Test Profile Wall Categories"])
    lines.append(
        md_row(["run", "action processing", "remote cache check", "remote execution", "remote process", "profile json"])
    )
    lines.append(md_row(["---", "---:", "---:", "---:", "---:", "---"]))
    for summary in summaries:
        test = invocation_by_role(summary, "test")
        profile = test.get("profile_summary", {})
        wall = profile.get("wall_by_category_s", {})
        lines.append(
            md_row(
                [
                    str(summary["run"]["databaseId"]),
                    fmt_s(wall.get("action_processing")),
                    fmt_s(wall.get("remote_action_cache_check")),
                    fmt_s(wall.get("remote_action_execution")),
                    fmt_s(wall.get("remote_execution_process_wall")),
                    str(test.get("profile_json", "")),
                ]
            )
        )

    action_stats: dict[tuple[str, str], list[float]] = {}
    for summary in summaries:
        test = invocation_by_role(summary, "test")
        profile = test.get("profile_summary", {})
        for action in profile.get("top_actions", [])[:5]:
            key = (action.get("mnemonic", ""), action.get("target", "") or action.get("name", ""))
            action_stats.setdefault(key, []).append(float(action["duration_s"]))

    lines.extend(["", "## Repeated Slow Actions"])
    lines.append(md_row(["mnemonic", "target/name", "runs", "median s", "max s"]))
    lines.append(md_row(["---", "---", "---:", "---:", "---:"]))
    for (mnemonic, target), durations in sorted(
        action_stats.items(), key=lambda item: (len(item[1]), max(item[1])), reverse=True
    ):
        lines.append(
            md_row(
                [mnemonic, target.replace("|", "\\|"), str(len(durations)), median(durations), f"{max(durations):.2f}"]
            )
        )

    lines.append("")
    lines.append("## Slowest Actions")
    for summary in summaries:
        lines.append("")
        lines.append(f"### Run {summary['run']['databaseId']} - {summary['run']['displayTitle']}")
        for invocation in summary["invocations"]:
            profile = invocation.get("profile_summary")
            if not profile:
                continue
            lines.append(f"- `{invocation['role']}` `{invocation['id']}` elapsed={invocation.get('elapsed_s')}s")
            for action in profile["top_actions"][:5]:
                target = f" `{action['target']}`" if action["target"] else ""
                lines.append(f"  - {action['duration_s']:.2f}s `{action['mnemonic']}`{target}: {action['name']}")
    out.write_text("\n".join(lines) + "\n")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("run_ids", nargs="+", help="GitHub Actions CI run IDs")
    p.add_argument("--repo", default="agentydragon/ducktape")
    p.add_argument("--out", type=Path, default=Path("/tmp/ci-build-profile-data/runs"))
    p.add_argument(
        "--bbapi",
        type=Path,
        default=Path("bazel-bin/devinfra/buildbuddy_cli/bbapi_/bbapi"),
        help="Path to repo-local bbapi binary with tool-log support",
    )
    p.add_argument(
        "--write-md", action="store_true", help="Also write aggregate.md as a human-readable derived summary"
    )
    return p


def main() -> None:
    args = parser().parse_args()
    summaries = [collect_run(args.repo, run_id, args.out, args.bbapi) for run_id in args.run_ids]
    aggregate_json = args.out / "aggregate.json"
    aggregate_json.write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")
    print(aggregate_json)
    if args.write_md:
        aggregate_md = args.out / "aggregate.md"
        write_aggregate(summaries, aggregate_md)
        print(aggregate_md)


if __name__ == "__main__":
    main()
