"""Run matched, uncached native Debundle CLI phases on one frozen corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from python.runfiles import Runfiles

from devinfra.js.debundle.excalidraw.summarize_tree import summarize_tree

GIB = 1024**3
ADDRESS_SPACE_LIMIT_BYTES = 16 * GIB
FILE_SIZE_LIMIT_BYTES = 512 * 1024**2
PHASE_TIMEOUT_SECONDS = 30 * 60
PROFILE_TIMEOUT_SECONDS = 45 * 60
TIME_FORMAT = '{"elapsed_seconds":%e,"user_seconds":%U,"system_seconds":%S,"max_rss_kib":%M,"exit_status":%x}'
TOOL_NAMES = {"projector": "debundle_project_analysis", "analyzer": "debundle_analyze", "debundler": "debundle"}


@dataclass(frozen=True)
class Resources:
    elapsed_seconds: float
    user_seconds: float
    system_seconds: float
    max_rss_kib: int
    exit_status: int


def parse_time_json(raw: str) -> Resources:
    """Parse the GNU time record written by the measured child process."""
    value = json.loads(raw)
    return Resources(
        elapsed_seconds=float(value["elapsed_seconds"]),
        user_seconds=float(value["user_seconds"]),
        system_seconds=float(value["system_seconds"]),
        max_rss_kib=int(value["max_rss_kib"]),
        exit_status=int(value["exit_status"]),
    )


def limited_command(argv: list[str], *, time_binary: str, time_output: Path, timeout_seconds: int) -> list[str]:
    """Build the measured command with enforced per-process limits."""
    return [
        *bounded_command(
            [time_binary, "--quiet", "--format", TIME_FORMAT, "--output", str(time_output), *argv], timeout_seconds
        )
    ]


def bounded_command(argv: list[str], timeout_seconds: int) -> list[str]:
    """Enforce timeout, address-space and output-file limits on a child tree."""
    return [
        "timeout",
        "--signal=TERM",
        "--kill-after=15s",
        f"{timeout_seconds}s",
        "prlimit",
        f"--as={ADDRESS_SPACE_LIMIT_BYTES}",
        f"--fsize={FILE_SIZE_LIMIT_BYTES}",
        "--",
        *argv,
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_manifest(root: Path) -> list[dict[str, object]]:
    return [
        {"path": path.relative_to(root).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def manifest_digest(files: list[dict[str, object]]) -> str:
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def copy_physical(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination, follow_symlinks=True)


def resolve_runfile(runfiles: Runfiles, env_name: str) -> Path:
    path = runfiles.Rlocation(os.environ[env_name])
    if path is None:
        raise ValueError(f"runfile could not be resolved: {env_name}")
    return Path(str(path)).resolve(strict=True)


def stage_inputs(workspace: Path, output_dir: Path, runfiles: Runfiles) -> dict[str, Any]:
    """Copy the public bundle, spec and vendor roots into one physical closure."""
    closure = output_dir / "inputs"
    bazel_bin = closure / "bazel-bin"
    package_root = closure / "packages"
    spec_root = closure / "spec" / "devinfra/js/debundle/excalidraw/spec"
    bundle_root = bazel_bin / "devinfra/js/debundle/excalidraw"

    config_source = resolve_runfile(runfiles, "DEBUNDLE_BENCH_CONFIG")
    copy_physical(config_source, bundle_root / "config.yaml")
    copy_physical(resolve_runfile(runfiles, "DEBUNDLE_BENCH_JS_LIST"), bundle_root / "js-files.txt")
    copy_physical(config_source.parent / "chunks", bundle_root / "chunks")
    copy_physical(workspace / "devinfra/js/debundle/excalidraw/spec", spec_root)
    copy_physical(resolve_runfile(runfiles, "DEBUNDLE_BENCH_CLSX"), package_root / "clsx")
    copy_physical(resolve_runfile(runfiles, "DEBUNDLE_BENCH_ROUGHJS"), package_root / "roughjs")

    files = tree_manifest(closure)
    identity = manifest_digest(files)
    spec_files = [
        item for item in files if cast(str, item["path"]).startswith("spec/devinfra/js/debundle/excalidraw/spec/")
    ]
    result = {
        "root": str(closure),
        "sha256": identity,
        "file_count": len(files),
        "total_bytes": sum(cast(int, item["size_bytes"]) for item in files),
        "files": files,
        "spec_sha256": manifest_digest(spec_files),
        "spec_file_count": len(spec_files),
    }
    (output_dir / "input_manifest.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return {
        "closure": closure,
        "bazel_bin": bazel_bin,
        "config": bundle_root / "config.yaml",
        "js_list": bundle_root / "js-files.txt",
        "chunks": bundle_root / "chunks",
        "modules": spec_root / "modules",
        "vendor_marks": spec_root / "vendor_marks.yaml",
        "package_roots": {"clsx": package_root / "clsx", "roughjs": package_root / "roughjs"},
        "manifest": result,
    }


def cgroup_snapshot() -> dict[str, Any]:
    """Read the outer runner cgroup's observable memory values when present."""
    values: dict[str, int | str | None] = {
        "runner_cgroup_memory_limit_bytes": None,
        "runner_cgroup_memory_peak_bytes": None,
        "oom_kill_count": None,
        "scope": "outer_runner_cgroup_observation",
    }
    root = Path("/sys/fs/cgroup")
    for key, filename in (
        ("runner_cgroup_memory_limit_bytes", "memory.max"),
        ("runner_cgroup_memory_peak_bytes", "memory.peak"),
    ):
        try:
            raw = (root / filename).read_text().strip()
        except OSError:
            continue
        if raw != "max":
            values[key] = int(raw)
    try:
        events = (root / "memory.events").read_text().splitlines()
    except OSError:
        return values
    values["oom_kill_count"] = next((int(line.split()[1]) for line in events if line.startswith("oom_kill ")), None)
    return values


def machine_info(env: dict[str, str]) -> dict[str, Any]:
    affinity = os.sched_getaffinity(0) if hasattr(os, "sched_getaffinity") else None
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "logical_cpus": os.cpu_count(),
        "allowed_cpus": len(affinity) if affinity is not None else None,
        "rayon_num_threads": env.get("RAYON_NUM_THREADS"),
        "runner_image": env.get("DEBUNDLE_BENCH_RUNNER_IMAGE"),
        "cgroup": cgroup_snapshot(),
    }


def tool_identity(engine: str, binary_dir: Path, revision: str) -> dict[str, Any]:
    tools = {}
    for role, name in TOOL_NAMES.items():
        path = binary_dir / name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise FileNotFoundError(f"{role} executable is missing or not executable: {path}")
        tools[role] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
    return {"engine": engine, "revision": revision, "tools": tools}


def package_args(package_roots: dict[str, Path]) -> list[str]:
    return [item for name, root in package_roots.items() for item in ("--package-root", f"{name}={root}")]


def phase_commands(inputs: dict[str, Any], run_dir: Path, binaries: dict[str, Path]) -> dict[str, list[str]]:
    common = [
        "--tree-config",
        str(inputs["config"]),
        "--tree-modules",
        str(inputs["modules"]),
        "--tree-vendor-marks",
        str(inputs["vendor_marks"]),
        "--tree-source-root",
        str(inputs["bazel_bin"]),
        *package_args(inputs["package_roots"]),
    ]
    projection = run_dir / "analysis.projection"
    apply_projection = run_dir / "apply.projection"
    analysis = run_dir / "source.analysis"
    apply_out = run_dir / "apply"
    projection_out = run_dir / "projection-out"
    return {
        "project": [
            str(binaries["projector"]),
            *common,
            "--out-root",
            str(projection_out),
            "--out",
            str(projection),
            "--apply-out",
            str(apply_projection),
            "--projection-root",
            str(inputs["closure"]),
        ],
        "analyze": [
            str(binaries["analyzer"]),
            "--projection",
            str(projection),
            "--tree-source-root",
            str(inputs["bazel_bin"]),
            *package_args(inputs["package_roots"]),
            "--out",
            str(analysis),
        ],
        "apply": [
            str(binaries["debundler"]),
            "run",
            *common,
            "--out-root",
            str(apply_out),
            "--analysis",
            str(analysis),
            "--apply-projection",
            str(apply_projection),
            "--projection-root",
            str(inputs["closure"]),
        ],
    }


def classify_status(status: int, before: dict[str, int | None], after: dict[str, int | None], stderr: str = "") -> str:
    if status == 0:
        return "succeeded"
    if status == 124:
        return "timed_out"
    if status == 137:
        previous = before.get("oom_kill_count")
        current = after.get("oom_kill_count")
        if previous is not None and current is not None and current > previous:
            return "cgroup_oom_kill"
        return "sigkill_without_cgroup_oom_evidence"
    if status in (126, 127):
        return "launch_failed"
    if re.search(
        r"(?:memory allocation of .{1,80} failed|failed to allocate|cannot allocate memory|out of memory)",
        stderr,
        re.IGNORECASE,
    ):
        return "allocation_failure"
    if 128 <= status <= 255:
        return "signal_or_signal_encoded_exit_status"
    return "child_failed"


def run_command(
    *,
    argv: list[str],
    phase: str,
    engine: str,
    output_dir: Path,
    time_binary: str,
    env: dict[str, str],
    timeout_seconds: int = PHASE_TIMEOUT_SECONDS,
    prefix: list[str] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    time_output = output_dir / "gnu_time.json"
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    before = cgroup_snapshot()
    command = limited_command(argv, time_binary=time_binary, time_output=time_output, timeout_seconds=timeout_seconds)
    if prefix is not None:
        # The prefix runs under the same timeout, address-space and file-size limits.
        command = limited_command(
            [*prefix, *argv], time_binary=time_binary, time_output=time_output, timeout_seconds=timeout_seconds
        )
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(command, env=env, stdout=stdout, stderr=stderr, check=False)
    after = cgroup_snapshot()
    with stderr_path.open("rb") as stderr:
        stderr.seek(max(0, stderr_path.stat().st_size - 65536))
        stderr_excerpt = stderr.read().decode(errors="replace")
    resources = None
    time_parse_error = None
    if time_output.exists():
        try:
            resources = parse_time_json(time_output.read_text())
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            time_parse_error = str(error)
    status = completed.returncode
    result = {
        "phase": phase,
        "engine": engine,
        "command": argv,
        "exit_status": status,
        "completion": classify_status(status, before, after, stderr_excerpt),
        "sample_complete": status == 0 and resources is not None and resources.exit_status == 0,
        "resources": (
            {
                "elapsed_seconds": resources.elapsed_seconds,
                "user_seconds": resources.user_seconds,
                "system_seconds": resources.system_seconds,
                "gnu_time_max_rss_kib": resources.max_rss_kib,
            }
            if resources is not None
            else None
        ),
        "cgroup_before": before,
        "cgroup_after": after,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "time_file": str(time_output) if time_output.exists() else None,
        "time_parse_error": time_parse_error,
    }
    (output_dir / "phase.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def valid_summary(summary: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    rejected = [
        chunk
        for chunk, result in summary["chunks"].items()
        if not result["gate_accepts"] or not result["translation_validation_accepts"]
    ]
    return bool(summary["chunks"]) and bool(summary["emitted"]) and not rejected, {
        "chunk_count": len(summary["chunks"]),
        "emitted_file_count": len(summary["emitted"]),
        "rejected_chunks": rejected,
    }


def summarize_samples(runs: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for phase in ("project", "analyze", "apply"):
        samples = [
            run["phases"][phase]["resources"]
            for run in runs
            if run["complete"]
            and run["phases"][phase]["sample_complete"]
            and run["phases"][phase]["resources"] is not None
        ]
        if not samples:
            result[phase] = {"n": 0}
            continue
        wall_times = [sample["elapsed_seconds"] for sample in samples]
        cpu_times = [sample["user_seconds"] + sample["system_seconds"] for sample in samples]
        rss_values = [sample["gnu_time_max_rss_kib"] for sample in samples]
        ordered = sorted(wall_times)
        p90_index = min(len(ordered) - 1, max(0, int(0.9 * len(ordered) + 0.999999) - 1))
        result[phase] = {
            "n": len(samples),
            "mean_seconds": statistics.fmean(wall_times),
            "p50_seconds": statistics.median(wall_times),
            "p90_seconds": ordered[p90_index],
            "mean_cpu_seconds": statistics.fmean(cpu_times),
            "p50_child_max_rss_kib": statistics.median(rss_values),
            "samples_seconds": wall_times,
        }
    return result


def write_summary(metrics: dict[str, Any], path: Path) -> None:
    lines = ["# Debundle performance run", "", f"Candidate: `{metrics['identity']['candidate_sha']}`"]
    baseline = metrics["identity"].get("baseline_sha")
    if baseline:
        lines.append(f"Baseline: `{baseline}`")
    lines.extend(
        [
            "",
            f"BuildBuddy runner image: `{metrics['machine']['runner_image']}`",
            "",
            f"Input closure SHA-256: `{metrics['input']['sha256']}` ({metrics['input']['file_count']} files)",
            "",
            "## Native CLI phase wall time",
            "",
            "| Engine | Phase | n | p50 wall (s) | p90 wall (s) | mean CPU (s) | p50 child max RSS (MiB) |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for engine, phases in metrics["summaries"].items():
        for phase, value in phases.items():
            if value["n"]:
                lines.append(
                    f"| {engine} | {phase} | {value['n']} | {value['p50_seconds']:.3f} | "
                    f"{value['p90_seconds']:.3f} | {value['mean_cpu_seconds']:.3f} | "
                    f"{value['p50_child_max_rss_kib'] / 1024:.1f} |"
                )
    comparison = metrics.get("output_comparison")
    if comparison is not None:
        lines.extend(
            [
                "",
                "## Output comparison",
                "",
                f"Canonical summaries identical: **{comparison['identical']}**.",
                f"Performance comparison eligible: **{comparison['performance_comparison_eligible']}**.",
                f"Changed emitted paths: {comparison['changed_emitted_file_count']}.",
            ]
        )
    lines.extend(
        [
            "",
            "Wall time excludes Bazel compilation and RBE queueing. GNU time's max RSS is the timed child command's maximum RSS; it is not aggregate process-tree peak RSS.",
            "Cgroup memory values are outer-runner snapshots; peak values are cumulative for the observed cgroup and are not attributed to an individual CLI phase.",
            "The 16 GiB limit is per-process virtual address space, not an aggregate RSS cap.",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def profile_analyze(
    *,
    mode: str,
    engine: str,
    binaries: dict[str, Path],
    inputs: dict[str, Any],
    analysis_projection: Path,
    output_dir: Path,
    runfiles: Runfiles,
    time_binary: str,
    env: dict[str, str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_binary = output_dir / "debundle_analyze"
    binary_size = binaries["analyzer"].stat().st_size
    if binary_size > FILE_SIZE_LIMIT_BYTES:
        raise ValueError(f"analyzer binary exceeds profile artifact limit: {binary_size} bytes")
    shutil.copy2(binaries["analyzer"], profile_binary)
    if sha256_file(profile_binary) != sha256_file(binaries["analyzer"]):
        raise RuntimeError("preserved analyzer binary did not match the profiled tool")
    analyzer_args = [
        str(profile_binary),
        "--projection",
        str(analysis_projection),
        "--tree-source-root",
        str(inputs["bazel_bin"]),
        *package_args(inputs["package_roots"]),
        "--out",
        str(output_dir / "profile.analysis"),
    ]
    if mode == "cpu":
        wrapper = resolve_runfile(runfiles, "DEBUNDLE_BENCH_PERF_WRAPPER")
        prefix = ["bash", str(wrapper), "--output-dir", str(output_dir / "perf"), "--"]
        profile_env = dict(env)
        profile_env.update(
            {
                "PERF_RECORD_FREQ": "49",
                "PERF_RECORD_EVENT": "cpu-clock:u",
                "PERF_CALL_GRAPH": "dwarf,8192",
                "PERF_POSTPROCESS_TIMEOUT": "30s",
                "PERF_CALLGRAPH_TIMEOUT": "10s",
                "PERF_CALLGRAPH_MAX_STACK": "48",
                "PERF_CALLGRAPH_SLICE_SECS": "4",
            }
        )
        result = run_command(
            argv=analyzer_args,
            phase="profile_analyze_cpu",
            engine=engine,
            output_dir=output_dir,
            time_binary=time_binary,
            env=profile_env,
            timeout_seconds=PROFILE_TIMEOUT_SECONDS,
            prefix=prefix,
        )
        failed_postprocessing = list((output_dir / "perf").glob("*.failed.txt"))
        if not (output_dir / "perf" / "perf.data").is_file():
            failed_postprocessing.append(output_dir / "perf" / "perf.data (missing)")
        if not (output_dir / "profile.analysis").is_file() or not (output_dir / "profile.analysis").stat().st_size:
            result["sample_complete"] = False
        result["profile_files"] = [str(path) for path in sorted((output_dir / "perf").glob("*"))]
        result["profile_postprocessing_failures"] = [str(path) for path in failed_postprocessing]
        record_stderr = output_dir / "perf" / "perf_record_stderr.txt"
        record_diagnostics = record_stderr.read_text(errors="replace") if record_stderr.is_file() else ""
        lost_samples = [
            int(value) for value in re.findall(r"(?:lost|dropped)\s+(\d+)", record_diagnostics, re.IGNORECASE)
        ]
        result["profile_settings"] = {
            "event": profile_env["PERF_RECORD_EVENT"],
            "frequency_hz": int(profile_env["PERF_RECORD_FREQ"]),
            "call_graph": profile_env["PERF_CALL_GRAPH"],
            "callers_may_be_incomplete_for_optimized_binary": True,
        }
        result["profile_capture_diagnostics"] = {
            "reported_lost_samples": sum(lost_samples) if lost_samples else None,
            "record_stderr_excerpt": record_diagnostics[-8192:],
        }
        result["profiled_analyzer"] = {"path": str(profile_binary), "sha256": sha256_file(profile_binary)}
        result["sample_complete"] = result["sample_complete"] and not failed_postprocessing
        return result

    massif_file = output_dir / "massif.out"
    valgrind = shutil.which("valgrind")
    if valgrind is None:
        raise FileNotFoundError("heap profile requested, but valgrind/Massif is not on PATH")
    result = run_command(
        argv=[
            valgrind,
            "--tool=massif",
            "--time-unit=B",
            "--max-snapshots=128",
            "--detailed-freq=10",
            "--massif-out-file=" + str(massif_file),
            *analyzer_args,
        ],
        phase="profile_analyze_heap",
        engine=engine,
        output_dir=output_dir,
        time_binary=time_binary,
        env=env,
        timeout_seconds=PROFILE_TIMEOUT_SECONDS,
    )
    if result["sample_complete"] and massif_file.is_file() and massif_file.stat().st_size > 0:
        ms_print = shutil.which("ms_print")
        if ms_print is None:
            raise FileNotFoundError("heap profile requested, but ms_print is not on PATH")
        summary = output_dir / "massif_summary.txt"
        with summary.open("wb") as stdout, (output_dir / "massif_summary.stderr.txt").open("wb") as stderr:
            postprocess = subprocess.run(
                [
                    "timeout",
                    "--signal=TERM",
                    "--kill-after=5s",
                    "30s",
                    "prlimit",
                    f"--as={ADDRESS_SPACE_LIMIT_BYTES}",
                    f"--fsize={FILE_SIZE_LIMIT_BYTES}",
                    "--",
                    ms_print,
                    str(massif_file),
                ],
                env=env,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
        result["postprocess_exit_status"] = postprocess.returncode
        if postprocess.returncode or not summary.stat().st_size:
            result["sample_complete"] = False
    else:
        result["sample_complete"] = False
    if not (output_dir / "profile.analysis").is_file() or not (output_dir / "profile.analysis").stat().st_size:
        result["sample_complete"] = False
    result["profile_files"] = [str(path) for path in sorted(output_dir.glob("massif*"))]
    result["profile_settings"] = {"tool": "valgrind-massif", "max_snapshots": 128, "time_unit": "bytes"}
    result["profiled_analyzer"] = {"path": str(profile_binary), "sha256": sha256_file(profile_binary)}
    return result


def main() -> None:
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--output-dir", type=Path)
    known, _ = early.parse_known_args()
    started_at = datetime.now(UTC)
    try:
        run_benchmark(started_at)
    except BaseException as error:
        if known.output_dir is not None:
            persist_failure(known.output_dir, started_at, error)
        raise


def persist_failure(output_dir: Path, started_at: datetime, error: BaseException) -> None:
    """Keep a machine-readable failure record when setup exits before metrics."""
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = output_dir / "metrics.json"
    try:
        metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
    except OSError, json.JSONDecodeError:
        metrics = {}
    metrics.update(
        {
            "started_at_utc": started_at.isoformat(),
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "complete": False,
            "failure": {"type": type(error).__name__, "message": str(error)},
        }
    )
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    summary = output_dir / "summary.md"
    previous = summary.read_text() if summary.is_file() else "# Debundle performance run\n"
    summary.write_text(previous + f"\nHarness failure: `{type(error).__name__}: {error}`\n")


def run_benchmark(started_at: datetime) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--candidate-binary-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--baseline-binary-dir", type=Path)
    parser.add_argument("--baseline-sha")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--profile-mode", choices=("none", "cpu", "heap"), default="none")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-summary", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 10:
        parser.error("--repetitions must be between 1 and 10")
    if (args.baseline_binary_dir is None) != (args.baseline_sha is None):
        parser.error("--baseline-binary-dir and --baseline-sha must be supplied together")
    if args.expected_summary is not None and not args.expected_summary.is_file():
        parser.error(f"--expected-summary does not exist: {args.expected_summary}")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in output_dir.iterdir():
        if item.name != "preparation":
            parser.error(f"output directory must be fresh: {output_dir}")

    runfiles = Runfiles.CreateOrRaise()
    time_binary = shutil.which("time")
    if time_binary is None:
        raise FileNotFoundError("GNU time is required on the BuildBuddy runner PATH")
    version = subprocess.run([time_binary, "--version"], capture_output=True, text=True, check=False)
    if version.returncode or "GNU time" not in version.stdout:
        raise RuntimeError(f"GNU time is required, found {time_binary}: {version.stdout} {version.stderr}")

    inputs = stage_inputs(args.workspace.resolve(), output_dir, runfiles)
    input_files_before = tree_manifest(inputs["closure"])
    input_digest = manifest_digest(input_files_before)
    engines = {
        "candidate": {
            "binaries": {role: args.candidate_binary_dir.resolve() / name for role, name in TOOL_NAMES.items()},
            "sha": args.candidate_sha,
        }
    }
    if args.baseline_binary_dir is not None and args.baseline_sha is not None:
        engines["baseline"] = {
            "binaries": {role: args.baseline_binary_dir.resolve() / name for role, name in TOOL_NAMES.items()},
            "sha": args.baseline_sha,
        }
    identities = {
        name: tool_identity(name, engine["binaries"]["projector"].parent, engine["sha"])
        for name, engine in engines.items()
    }
    env = dict(os.environ)
    env.pop("DEBUNDLE_OUT", None)
    env.pop("DEBUNDLE_TREE_SOURCE_ROOT", None)
    env.setdefault("RAYON_NUM_THREADS", "8")
    repetitions: list[dict[str, Any]] = []
    failed = False
    for index in range(args.repetitions):
        engine_names = list(engines)
        if len(engine_names) == 2 and index % 2:
            engine_names.reverse()
        for engine_name in engine_names:
            engine = engines[engine_name]
            run_dir = output_dir / "runs" / f"{engine_name}-{index + 1:02d}"
            binaries = engine["binaries"]
            commands = phase_commands(inputs, run_dir, binaries)
            run_record: dict[str, Any] = {
                "engine": engine_name,
                "repetition": index + 1,
                "complete": False,
                "phases": {},
            }
            repetitions.append(run_record)
            for phase, argv in commands.items():
                phase_record = run_command(
                    argv=argv,
                    phase=phase,
                    engine=engine_name,
                    output_dir=run_dir / phase,
                    time_binary=time_binary,
                    env=env,
                )
                run_record["phases"][phase] = phase_record
                if not phase_record["sample_complete"]:
                    failed = True
                    break
            if not failed:
                summary = summarize_tree(run_dir / "apply")
                accepted, evidence = valid_summary(summary)
                summary_path = run_dir / "canonical_summary.json"
                summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
                run_record["output"] = {
                    **evidence,
                    "accepted": accepted,
                    "summary_sha256": sha256_file(summary_path),
                    "summary_path": str(summary_path),
                }
                run_record["complete"] = accepted
                failed = not accepted
            if failed:
                break
        if failed:
            break

    profile_records = []
    if not failed and args.profile_mode in ("cpu", "heap"):
        for engine_name, engine in engines.items():
            first_run = next(run for run in repetitions if run["engine"] == engine_name)
            try:
                profile = profile_analyze(
                    mode=args.profile_mode,
                    engine=engine_name,
                    binaries=engine["binaries"],
                    inputs=inputs,
                    analysis_projection=Path(first_run["phases"]["project"]["stdout"]).parent.parent
                    / "analysis.projection",
                    output_dir=output_dir / "profiles" / engine_name,
                    runfiles=runfiles,
                    time_binary=time_binary,
                    env=env,
                )
            except Exception as error:
                profile = {
                    "engine": engine_name,
                    "mode": args.profile_mode,
                    "sample_complete": False,
                    "failure": {"type": type(error).__name__, "message": str(error)},
                }
            profile_records.append(profile)
            failed = failed or not profile["sample_complete"]

    input_files_after = tree_manifest(inputs["closure"])
    if manifest_digest(input_files_after) != input_digest:
        failed = True
    summaries: dict[str, Any] = {
        name: summarize_samples([run for run in repetitions if run["engine"] == name]) for name in engines
    }
    output_comparison = None
    if "baseline" in engines and repetitions:
        candidate = next(
            (run.get("output") for run in repetitions if run["engine"] == "candidate" and run.get("output")), None
        )
        baseline = next(
            (run.get("output") for run in repetitions if run["engine"] == "baseline" and run.get("output")), None
        )
        if candidate is not None and baseline is not None:
            candidate_summary = json.loads(Path(candidate["summary_path"]).read_text())
            baseline_summary = json.loads(Path(baseline["summary_path"]).read_text())
            changed_emitted = sorted(
                key
                for key in set(candidate_summary["emitted"]) | set(baseline_summary["emitted"])
                if candidate_summary["emitted"].get(key) != baseline_summary["emitted"].get(key)
            )
            output_comparison = {
                "identical": candidate_summary == baseline_summary,
                "performance_comparison_eligible": candidate_summary == baseline_summary,
                "changed_emitted_file_count": len(changed_emitted),
                "changed_emitted_files": changed_emitted[:200],
                "changed_chunk_count": sum(
                    candidate_summary["chunks"].get(key) != baseline_summary["chunks"].get(key)
                    for key in set(candidate_summary["chunks"]) | set(baseline_summary["chunks"])
                ),
            }
    expected_output_match = None
    if args.expected_summary is not None and any(
        run["engine"] == "candidate" and run.get("output") for run in repetitions
    ):
        expected = json.loads(args.expected_summary.read_text())
        candidate_summary = next(
            json.loads(Path(run["output"]["summary_path"]).read_text())
            for run in repetitions
            if run["engine"] == "candidate" and run.get("output")
        )
        expected_output_match = candidate_summary == expected
        if not expected_output_match:
            failed = True

    stable_summaries = {}
    for engine_name in engines:
        run_hashes = [
            run["output"]["summary_sha256"] for run in repetitions if run["engine"] == engine_name and run.get("output")
        ]
        stable_summaries[engine_name] = len(run_hashes) == args.repetitions and len(set(run_hashes)) == 1
    if not all(stable_summaries.values()):
        failed = True
    metrics = {
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "identity": {
            "workspace_sha": args.candidate_sha,
            "candidate_sha": args.candidate_sha,
            "baseline_sha": args.baseline_sha,
            "tools": identities,
        },
        "input": inputs["manifest"],
        "machine": machine_info(env),
        "resource_limits": {
            "kind": "address_space_per_process",
            "address_space_bytes": ADDRESS_SPACE_LIMIT_BYTES,
            "file_size_bytes_per_file": FILE_SIZE_LIMIT_BYTES,
            "phase_timeout_seconds": PHASE_TIMEOUT_SECONDS,
            "profile_timeout_seconds": PROFILE_TIMEOUT_SECONDS,
            "scope_note": "GNU time max RSS is for its measured child; it is not aggregate process-tree peak RSS.",
        },
        "profile_mode": args.profile_mode,
        "repetitions_requested": args.repetitions,
        "runs": repetitions,
        "stable_canonical_summaries": stable_summaries,
        "profiles": profile_records,
        "summaries": summaries,
        "output_comparison": output_comparison,
        "candidate_matches_bazel_canonical_summary": expected_output_match,
        "input_closure_unchanged": manifest_digest(input_files_after) == input_digest,
        "complete": not failed and all(run["complete"] for run in repetitions),
    }
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    write_summary(metrics, output_dir / "summary.md")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
