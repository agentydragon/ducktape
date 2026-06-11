#!/usr/bin/env -S env -u PYTHONPATH uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "inspect-ai",
#   "inspect-evals[swe_bench]",
#   "openai",
#   "swebench",
# ]
# ///
# The double-env-shebang clears PYTHONPATH before uv runs. Without it, Nix's
# devshell PYTHONPATH leaks into the uv-managed venv, and Python imports
# `pydantic` from the Nix store (incompatible with the venv's `pydantic_core`),
# producing `ModuleNotFoundError: pydantic_core._pydantic_core`.
"""N=100 SWE-bench Verified run against gpt-oss:20b on the cluster Ollama
endpoint via Inspect AI. Same script as the pilot at
<../2026-04-29_swebench_pilot_gpt20/run_swebench.py> with `DEFAULT_LIMIT = 100`.

Notes (carried from pilot):

1. SWE-bench is agentic. The canonical `inspect_evals/swe_bench` task
   uses `swe_bench_agent_with_inspect_tool_support` (multi-turn
   `bash_session` / `python` / `text_editor`), but `bash_session`'s
   `type` / `type_submit` distinction was confusing `gpt-oss:20b`
   (cf. transcripts in earlier runs). This script points at the local
   `swebench_react_task.py@swe_bench_react` wrapper, which swaps in
   `swe_bench_react_agent` (stateless `bash` + `python` + `think`).
   Per-problem wall time is 6-15 min in our setup.
2. Per-problem Docker images pulled on demand from
   `ghcr.io/epoch-research/swe-bench.eval.<arch>.<id>:latest`. ghcr.io
   requires auth; this script does `gh auth token | docker login
   ghcr.io` once at startup.
3. `reasoning_effort` does NOT flow through the SWE-bench task to the
   underlying generate calls.
4. INSPECT_SANDBOX_MAX_EXEC_OUTPUT_SIZE bumped to 1 GiB to work around
   inspect_ai's CircularByteBuffer corruption bug (see the pilot's
   upstream_issue.md).
5. **Open issue (next attempt's TODO):** Inspect AI doesn't pin a
   `max_tokens` for openai-compat backends. Ollama then sizes the KV
   cache as `prompt_tokens + max_tokens` rounded to the next 2x block,
   which on SWE-bench prompts lands at `num_ctx=262144` — over
   `gpt-oss:20b`'s 131 072-token training limit, so Ollama returns 500
   on every request. Add `--max-tokens 8192` to the `inspect eval`
   invocation below before re-launching. See
   `attempts/run2_react_num_ctx_500s/README.md` for the diagnosis.

Usage:
    ./run_swebench.py [--limit 100] [--dataset lite|verified] [--message-limit 50]
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
DEFAULT_LIMIT = 100
DEFAULT_DATASET = "verified"  # "lite" or "verified". Verified is the task default;
# Lite requires also overriding `revision` because the task pins a Verified-specific SHA
# (`c104f840…`) which doesn't exist in the Lite repo.
DEFAULT_MESSAGE_LIMIT = 1000
DEFAULT_SANDBOX = "docker"
DEFAULT_MAX_WORKERS = 2  # avoid Docker subnet exhaustion at higher concurrency
BASE_URL = "https://ollama.allegedly.works/v1"
SECRET_NAMESPACE = "ollama"
SECRET_NAME = "ollama-bearer-token"
GHCR_USERNAME = "agentydragon"

DATASET_HF_IDS = {"lite": "princeton-nlp/SWE-bench_Lite", "verified": "princeton-nlp/SWE-bench_Verified"}


def get_bearer_token() -> str:
    """Pull the Ollama bearer token from the cluster Secret via kubectl."""
    raw = subprocess.check_output(
        ["kubectl", "-n", SECRET_NAMESPACE, "get", "secret", SECRET_NAME, "-o", "jsonpath={.data.token}"]
    )
    return base64.b64decode(raw).decode().strip()


def ghcr_login() -> None:
    """Authenticate to ghcr.io via gh CLI token, so docker can pull SWE-bench images."""
    token = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
    proc = subprocess.run(
        ["docker", "login", "ghcr.io", "--username", GHCR_USERNAME, "--password-stdin"],
        input=token,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f"ERROR: docker login ghcr.io failed: {proc.stderr.strip()}")
    print(f"docker login ghcr.io: {proc.stdout.strip()}", flush=True)


def inspect_path() -> str:
    p = shutil.which("inspect")
    if not p:
        sys.exit(
            "ERROR: `inspect` CLI not found in PATH after dependency resolution. Is `inspect-ai` installed correctly?"
        )
    return p


def run_eval(
    *, model: str, limit: int, dataset: str, message_limit: int, max_workers: int, log_dir: Path, env: dict[str, str]
) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Local wrapper task that swaps the default `bash_session` solver for
    # `swe_bench_react_agent` (stateless `bash` + `python` + `think`). See
    # swebench_react_task.py for rationale.
    task_spec = str(Path(__file__).resolve().parent / "swebench_react_task.py") + "@swe_bench_react"
    cmd = [
        inspect_path(),
        "eval",
        task_spec,
        "--model",
        f"openai/{model}",
        "--limit",
        str(limit),
        "--message-limit",
        str(message_limit),
        "--sandbox",
        DEFAULT_SANDBOX,
        "--max-connections",
        str(max_workers),
        "--display",
        "plain",  # per-sample lines to stdout; works when stdout is redirected
        "--sample-shuffle",
        "42",  # SWE-bench Verified is alpha-ordered by repo; without shuffling,
        # any --limit < 500 over-samples astropy + django.
        "--log-dir",
        str(log_dir),
    ]
    # Only override dataset when not using the task default (Verified). The task
    # also pins a Verified-specific revision SHA, so for Lite we have to clear
    # the revision too — passing `main` falls back to HF's default branch.
    if dataset != "verified":
        cmd.extend(["-T", f"dataset={DATASET_HF_IDS[dataset]}", "-T", "revision=main"])
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, check=False).returncode


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--dataset", choices=list(DATASET_HF_IDS), default=DEFAULT_DATASET)
    p.add_argument("--message-limit", type=int, default=DEFAULT_MESSAGE_LIMIT)
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    p.add_argument("--out", default="eval_logs", help="Log directory (relative to script dir).")
    p.add_argument(
        "--skip-ghcr-login", action="store_true", help="Skip docker login ghcr.io (assume already authenticated)."
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    out_dir = script_dir / args.out

    if not args.skip_ghcr_login:
        ghcr_login()

    token = get_bearer_token()
    env = os.environ.copy()
    env["OPENAI_BASE_URL"] = BASE_URL
    env["OPENAI_API_KEY"] = token
    # Workaround for inspect_ai's CircularByteBuffer silently corrupting JSON-RPC
    # responses larger than MAX_EXEC_OUTPUT_SIZE (default 10 MiB). See
    # upstream_issue.md alongside this script. A single command in the
    # astropy__astropy-12907 sandbox produces ~18 MiB of stderr (`grep -R … ..`
    # walking /sys symlink cycles), which is enough to corrupt the wire on the
    # first call. Bumping to 1 GiB delays the failure for our case but doesn't
    # make the truncation safe; a long-enough run can still saturate.
    env.setdefault("INSPECT_SANDBOX_MAX_EXEC_OUTPUT_SIZE", str(1024 * 1024 * 1024))

    summary: dict = {
        "config": {
            "model": args.model,
            "limit": args.limit,
            "dataset": args.dataset,
            "dataset_hf_id": DATASET_HF_IDS[args.dataset],
            "message_limit": args.message_limit,
            "max_workers": args.max_workers,
            "sandbox": DEFAULT_SANDBOX,
            "base_url": BASE_URL,
        }
    }

    rc = run_eval(
        model=args.model,
        limit=args.limit,
        dataset=args.dataset,
        message_limit=args.message_limit,
        max_workers=args.max_workers,
        log_dir=out_dir,
        env=env,
    )
    summary["exit_code"] = rc

    (script_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n=== DONE ===")
    print(json.dumps(summary, indent=2))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
