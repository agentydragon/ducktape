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
"""Run AIME-2024 against gpt-oss:20b on the cluster Ollama endpoint via
Inspect AI, sweeping `reasoning_effort` ∈ {low, medium, high}.

Driven from out-of-cluster (uses the public `ollama.allegedly.works`
endpoint with the bearer token from the in-cluster Secret) so we don't
have to fight the mitmproxy auto-injection in `claude-sandbox`.

Usage:
    ./run_aime.py [--limit 10] [--efforts low,medium,high] [--model gpt-oss:20b]

Output:
    logs/<effort>/...  Inspect AI per-run logs (commit alongside this script)
    summary.json       Headline numbers (pass@1 etc.) extracted from the logs
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
DEFAULT_LIMIT = 30
BASE_URL = "https://ollama.allegedly.works/v1"
SECRET_NAMESPACE = "ollama"
SECRET_NAME = "ollama-bearer-token"


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


def run_one(*, effort: str, model: str, limit: int, log_dir: Path, env: dict[str, str]) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        inspect_path(),
        "eval",
        "inspect_evals/aime2024",
        "--model",
        f"openai/{model}",
        "--limit",
        str(limit),
        "--reasoning-effort",
        effort,
        "--log-dir",
        str(log_dir),
    ]
    print(f"\n=== effort={effort} ===\n$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, check=False).returncode


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--efforts", default=DEFAULT_EFFORTS, help="Comma-separated reasoning_effort values to sweep.")
    p.add_argument("--out", default="eval_logs", help="Per-effort log directory (relative to script dir).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    out_root = script_dir / args.out

    token = get_bearer_token()
    env = os.environ.copy()
    env["OPENAI_BASE_URL"] = BASE_URL
    env["OPENAI_API_KEY"] = token

    summary: dict[str, dict] = {
        "config": {"model": args.model, "limit": args.limit, "efforts": args.efforts.split(","), "base_url": BASE_URL},
        "exit_codes": {},
    }

    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    for effort in efforts:
        rc = run_one(effort=effort, model=args.model, limit=args.limit, log_dir=out_root / effort, env=env)
        summary["exit_codes"][effort] = rc

    (script_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n=== ALL DONE ===")
    print(json.dumps(summary, indent=2))
    return 0 if all(rc == 0 for rc in summary["exit_codes"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
