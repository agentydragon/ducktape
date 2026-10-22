#!/usr/bin/env python3
"""Run the pinned OpenCode task with Ollama and the agent inside one net namespace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
RESULTS_DIR = RUN_DIR / "results"
DEFAULT_OLLAMA = "/nix/store/845p22skhnydsa22k9c7fq5sgi22pxli-ollama-0.40.2/bin/ollama"
DEFAULT_BWRAP = "/nix/store/bs0izr20qsssmygsg7qzis680ncifdil-bubblewrap-0.12.0/bin/bwrap"
DEFAULT_MODEL_CACHE = Path("/tmp/ducktape-rugged-models")
DEFAULT_LAUNCHER = Path(
    "/nix/store/hbl5rzd74qs9z679kjq7wsj6x2lcpmd9-rugged-opencode-offline/bin/rugged-opencode-offline"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ollama", type=Path, default=Path(DEFAULT_OLLAMA))
    parser.add_argument("--model-cache", type=Path, default=DEFAULT_MODEL_CACHE)
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    parser.add_argument("--bwrap", type=Path, default=Path(DEFAULT_BWRAP))
    parser.add_argument("--backend-port", type=int, default=11501)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.timeout_seconds < 1 or not 1024 <= args.backend_port <= 65535:
        parser.error("timeout must be positive and backend port must be unprivileged")
    for name in ("ollama", "model_cache", "launcher", "bwrap"):
        path = getattr(args, name).resolve(strict=True)
        if name == "model_cache" and not path.is_dir():
            parser.error("--model-cache must name a directory")
        if name != "model_cache" and not path.is_file():
            parser.error(f"--{name} must name a file")
        setattr(args, name, path)
    args.task_dir = RESULTS_DIR / args.run_id
    if args.task_dir.exists():
        parser.error(f"task output directory already exists: {args.task_dir}")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    args.harness_source = RUN_DIR
    return args


def main() -> int:
    args = parse_args()
    run_log = RESULTS_DIR / f"fully-offline-{args.run_id}"
    stdout_path = Path(f"{run_log}.stdout.log")
    stderr_path = Path(f"{run_log}.stderr.log")
    # Existing root mountpoints let us bind after the root is made read-only.
    # Replacing /var/tmp with only this fresh harness avoids leaking prior answers.
    harness_mount = "/var/tmp"
    scratch = Path(tempfile.mkdtemp(prefix="rugged-fully-offline-task-"))
    scratch_harness = scratch / "smoke_7910"
    scratch_harness.mkdir()
    shutil.copy2(args.harness_source / "run_opencode_smoke.py", scratch_harness)
    shutil.copy2(args.harness_source / "selected_task.json", scratch_harness)
    shutil.copytree(args.harness_source / "jsonl-transform", scratch_harness / "jsonl-transform")
    (scratch_harness / "results").mkdir()
    bootstrap = (RUN_DIR / "offline_task_bootstrap.py").read_text()
    command = [
        str(args.bwrap),
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--dir",
        "/dev/dri",
        "--dev-bind",
        "/dev/dri",
        "/dev/dri",
        "--proc",
        "/proc",
        "--bind",
        str(scratch_harness),
        harness_mount,
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/root",
        "--dir",
        "/tmp/offline-home",
        "--ro-bind",
        str(args.model_cache),
        "/mnt",
        "--clearenv",
        "--setenv",
        "PATH",
        os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
        "--setenv",
        "HOME",
        "/tmp/offline-home",
        "--setenv",
        "LANG",
        os.environ.get("LANG", "C.UTF-8"),
        "--chdir",
        "/",
        "--",
        sys.executable,
        "-c",
        bootstrap,
        str(args.ollama),
        str(args.launcher),
        f"{harness_mount}/run_opencode_smoke.py",
        args.run_id,
        str(args.backend_port),
        str(args.timeout_seconds),
        f"{harness_mount}/results",
    ]
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        completed = subprocess.run(command, check=False, stdout=stdout, stderr=stderr)
    if (scratch_harness / "results" / args.run_id).exists():
        shutil.copytree(scratch_harness / "results" / args.run_id, args.task_dir)
    for artifact_name in (f"offline-proof-{args.run_id}.json", f"ollama-server-{args.run_id}.log"):
        source = scratch_harness / "results" / artifact_name
        if source.exists():
            shutil.copy2(source, RESULTS_DIR / artifact_name)
    shutil.rmtree(scratch)
    print(
        json.dumps(
            {
                "exit_code": completed.returncode,
                "task_dir": str(args.task_dir),
                "proof": str(RESULTS_DIR / f"offline-proof-{args.run_id}.json"),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            },
            indent=2,
        )
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
