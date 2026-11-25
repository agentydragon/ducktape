"""Measure checker strategies while preserving a test's ordinary lowersplit result."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


def digest_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def input_id(manifest_path: Path, out: Path, check_only: bool, configuration: list[str]) -> str:
    try:
        manifest_text = manifest_path.read_text()
    except OSError:
        return hashlib.sha256(str(manifest_path).encode()).hexdigest()[:16]
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        return hashlib.sha256(manifest_text.encode()).hexdigest()[:16]

    chunks = []
    for chunk in manifest.get("chunks", []):
        row = {"path": chunk.get("path")}
        for field in ("source", "spec", "placement"):
            value = chunk.get(field)
            if isinstance(value, str):
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = manifest_path.parent / candidate
                row[field] = digest_file(candidate) or value
            elif value is not None:
                row[field] = value
        chunks.append(row)
    output_digest = None
    if check_only and out.exists():
        digest = hashlib.sha256()
        for file in sorted(path for path in out.rglob("*") if path.is_file()):
            digest.update(file.relative_to(out).as_posix().encode())
            digest.update(file.read_bytes())
        output_digest = digest.hexdigest()
    identity = json.dumps(
        {
            "entry": manifest.get("entry"),
            "chunks": chunks,
            "checked_output": output_digest,
            "configuration": configuration,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def verdict_from(result: subprocess.CompletedProcess[bytes]) -> dict:
    for line in reversed(result.stdout.decode(errors="replace").splitlines()):
        try:
            verdict = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(verdict, dict) and "status" in verdict:
            return verdict
    return {}


def run_attempt(
    executable: str, args: list[str], strategy: str, original_out: Path, check_only: bool
) -> tuple[dict, float, str]:
    out = Path(os.environ["TEST_TMPDIR"]) / ".split-strategy-census" / uuid.uuid4().hex / "out"
    out.parent.mkdir(parents=True, exist_ok=True)
    if check_only and original_out.exists():
        shutil.copytree(original_out, out)
    command_args = list(args)
    command_args[1] = str(out)
    child_env = os.environ.copy()
    child_env["SPLIT_PROOF_CENSUS_STRATEGY"] = strategy
    started = time.perf_counter()
    result = subprocess.run([executable, *command_args], check=False, capture_output=True, env=child_env)
    elapsed_ms = (time.perf_counter() - started) * 1000
    verdict = verdict_from(result)
    if not verdict:
        verdict = {
            "status": "error",
            "reason": (result.stderr.decode(errors="replace") or "checker returned no JSON verdict")[-4000:],
        }
    return verdict, elapsed_ms, (result.stderr.decode(errors="replace")[-4000:])


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def record_attempt(report: Path, case: str, input_name: str, strategy: str, verdict: dict, elapsed_ms: float) -> None:
    append_record(
        report,
        {
            "input": case,
            "input_name": input_name,
            "case_name": os.environ.get("SPLIT_PROOF_STRATEGY_CENSUS_CASE", input_name),
            "strategy": strategy,
            "certificate_strategy": verdict.get("strategy"),
            "status": verdict.get("status", "error"),
            "scope": verdict.get("scope"),
            "guarantee": verdict.get("guarantee"),
            "reason_code": verdict.get("reason_code"),
            "reason": verdict.get("reason"),
            "replay_blockers": verdict.get("replay_blockers"),
            "elapsed_ms": round(elapsed_ms, 2),
            "requested": verdict.get("requested"),
            "test": Path(os.environ.get("TEST_BINARY", "unknown")).name,
        },
    )


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: strategy_census_wrapper.py <lowersplit> <manifest> <outdir> [options]")
    executable, *args = sys.argv[1:]
    if len(args) < 2:
        return subprocess.run([executable, *args], check=False).returncode

    report_value = os.environ.get("SPLIT_PROOF_STRATEGY_CENSUS_OUT") or os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if not report_value:
        return subprocess.run([executable, *args], check=False).returncode
    report = Path(report_value)
    if report.is_dir() or report.suffix != ".jsonl":
        report = report / "split_strategy_census.jsonl"

    manifest = Path(args[0])
    original_out = Path(args[1])
    if not original_out.is_absolute():
        original_out = Path.cwd() / original_out
    check_only = "--check-output" in args
    configuration = args[2:]
    case = input_id(manifest, original_out, check_only, configuration)
    input_name = manifest.stem
    try:
        manifest_data = json.loads(manifest.read_text())
        is_singleton = len(manifest_data.get("chunks", [])) == 1
    except OSError:
        is_singleton = True
    except json.JSONDecodeError:
        is_singleton = True
    started = time.perf_counter()
    baseline = subprocess.run([executable, *args], check=False, capture_output=True)
    baseline_ms = (time.perf_counter() - started) * 1000
    baseline_verdict = verdict_from(baseline)
    if not baseline_verdict:
        baseline_verdict = {
            "status": "error",
            "reason": (baseline.stderr.decode(errors="replace") or "checker returned no JSON verdict")[-4000:],
        }
    record_attempt(report, case, input_name, "automatic", baseline_verdict, baseline_ms)

    strategies = ["structural"]
    if is_singleton:
        strategies.extend(("compiled-replay-premised", "generated-proof"))
    for strategy in strategies:
        verdict, elapsed_ms, _ = run_attempt(executable, args, strategy, original_out, check_only)
        record_attempt(report, case, input_name, strategy, verdict, elapsed_ms)

    sys.stdout.buffer.write(baseline.stdout)
    sys.stderr.buffer.write(baseline.stderr)
    return baseline.returncode


if __name__ == "__main__":
    raise SystemExit(main())
