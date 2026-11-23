#!/usr/bin/env -S env -u PYTHONPATH uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "inspect-ai",
#   "inspect-evals",
#   "openai",
# ]
# ///
# The double-env-shebang clears PYTHONPATH before uv runs. Without it, Nix's
# devshell PYTHONPATH leaks into the uv-managed venv, and Python imports
# `pydantic` from the Nix store (incompatible with the venv's `pydantic_core`),
# producing `ModuleNotFoundError: pydantic_core._pydantic_core`.
"""Sweep reasoning effort for Inspect AI's AIME-2024 or HumanEval task.

Run from the record directory (or pass --run-dir). Each effort writes an
Inspect log under --out; summary.json records the configuration and exit codes.
Examples:
    ../run_inspect_sweep.py aime2024 --limit 30
    ../run_inspect_sweep.py humaneval --limit 5 --efforts medium
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_MODEL = "gpt-oss:20b"
DEFAULT_EFFORTS = "low,medium,high"
BASE_URL = "https://ollama.allegedly.works/v1"
SECRET_NAMESPACE = "ollama"
SECRET_NAME = "ollama-direct-token"


def get_bearer_token() -> str:
    """Pull the Ollama bearer token from the cluster Secret via kubectl."""
    raw = subprocess.check_output(
        ["kubectl", "-n", SECRET_NAMESPACE, "get", "secret", SECRET_NAME, "-o", "jsonpath={.data.token}"]
    )
    return base64.b64decode(raw).decode().strip()


def inspect_path() -> str:
    p = shutil.which("inspect")
    if not p:
        sys.exit(
            "ERROR: `inspect` CLI not found in PATH after dependency resolution. Is `inspect-ai` installed correctly?"
        )
    return p


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    tasks = p.add_subparsers(dest="task", required=True)
    aime = tasks.add_parser("aime2024")
    aime.add_argument("--limit", type=int, required=True)
    humaneval = tasks.add_parser("humaneval")
    humaneval.add_argument("--limit", type=int, default=164)
    humaneval.add_argument("--sandbox", default="docker", help="Inspect sandbox for executing generated Python.")
    for task in (aime, humaneval):
        task.add_argument("--model", default=DEFAULT_MODEL)
        task.add_argument("--run-dir", type=Path, default=Path(), help="Existing run record directory.")
        task.add_argument("--efforts", default=DEFAULT_EFFORTS, help="Comma-separated reasoning efforts.")
        task.add_argument("--out", type=Path, default=Path("eval_logs"), help="Log directory relative to --run-dir.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve(strict=True)
    if not run_dir.is_dir():
        raise NotADirectoryError(run_dir)
    out_root = run_dir / args.out

    token = get_bearer_token()
    env = os.environ.copy()
    env["OPENAI_BASE_URL"] = BASE_URL
    env["OPENAI_API_KEY"] = token

    summary: dict[str, dict] = {
        "config": {"model": args.model, "limit": args.limit, "efforts": args.efforts.split(","), "base_url": BASE_URL},
        "exit_codes": {},
    }

    sandbox_args = []
    if args.task == "humaneval":
        sandbox_args = ["--sandbox", args.sandbox]
        summary["config"]["sandbox"] = args.sandbox

    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    for effort in efforts:
        log_dir = out_root / effort
        log_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            inspect_path(),
            "eval",
            f"inspect_evals/{args.task}",
            "--model",
            f"openai/{args.model}",
            "--limit",
            str(args.limit),
            "--reasoning-effort",
            effort,
            *sandbox_args,
            "--log-dir",
            str(log_dir),
        ]
        print(f"\n=== effort={effort} ===\n$ " + " ".join(cmd), flush=True)
        summary["exit_codes"][effort] = subprocess.run(cmd, env=env, check=False).returncode

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n=== ALL DONE ===")
    print(json.dumps(summary, indent=2))
    return 0 if all(rc == 0 for rc in summary["exit_codes"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
