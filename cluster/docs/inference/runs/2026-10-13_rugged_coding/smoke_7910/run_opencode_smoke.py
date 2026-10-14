#!/usr/bin/env python3
"""Run one pinned #7910 task with a local-only OpenCode tool profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
SOURCE_WORKSPACE = RUN_DIR / "jsonl-transform"
MODEL_ID = "qwen3:4b-instruct-2507-q4_K_M"
SOURCE_COMMIT = "283d234eee27cff5e5d8fbb5e34fd4e3560d796b"
FIXTURE_SHA256 = "b2706b6f910dfbcbec4e34abc92c886ee9de44c12e4dbac020825c8b9758be66"


def _ollama(port: int, path: str) -> dict[str, object]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return json.load(response)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _materialize_verifier(workspace: Path) -> None:
    task_metadata = json.loads((RUN_DIR / "selected_task.json").read_text())
    verifier_data = workspace / "tests" / "test_task.py.txt"
    verifier = workspace / "tests" / "test_task.py"
    verifier_bytes = verifier_data.read_bytes()
    if hashlib.sha256(verifier_bytes).hexdigest() != task_metadata["verifier_sha256"]:
        raise ValueError("frozen PR #7910 verifier bytes do not match the manifest")
    verifier.write_bytes(verifier_bytes)
    verifier_data.unlink()
    prompt_bytes = task_metadata["prompt"].encode("utf-8")
    if hashlib.sha256(prompt_bytes).hexdigest() != task_metadata["prompt_sha256"]:
        raise ValueError("frozen PR #7910 prompt bytes do not match the manifest")
    (workspace / "prompt.txt").write_bytes(prompt_bytes)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend-port", type=int, default=11500)
    parser.add_argument("--launcher", type=Path, required=True, help="built rugged-opencode-offline executable")
    parser.add_argument("--model", default=MODEL_ID, help="recording only; the launcher profile selects the model")
    args = parser.parse_args()
    args.result_dir = RUN_DIR / "results" / args.run_id
    args.workspace = args.result_dir / "workspace"
    if args.result_dir.exists():
        parser.error(f"run directory already exists: {args.result_dir}")
    return args


def main() -> None:
    args = _parse_args()
    args.result_dir.mkdir(parents=True)
    shutil.copytree(SOURCE_WORKSPACE, args.workspace)
    _materialize_verifier(args.workspace)
    baseline_tests_hash = _sha256(args.workspace / "tests" / "test_task.py")
    prompt = (args.workspace / "prompt.txt").read_text()
    (args.result_dir / "ollama_ps_before.json").write_text(
        json.dumps(_ollama(args.backend_port, "/api/ps"), indent=2) + "\n"
    )
    command = [
        str(args.launcher.resolve(strict=True)),
        "run",
        "--dir",
        "/tmp/offline-workspace",
        "--auto",
        "--format",
        "json",
        "--title",
        "Rugged offline JSONL transform coding smoke",
        prompt,
    ]
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    with (
        (args.result_dir / "opencode.stdout.jsonl").open("w") as stdout,
        (args.result_dir / "opencode.stderr.log").open("w") as stderr,
    ):
        env = {**os.environ, "RUGGED_OPENCODE_BACKEND_PORT": str(args.backend_port)}
        completed = subprocess.run(command, stdout=stdout, stderr=stderr, check=False, cwd=args.workspace, env=env)
    wall_seconds = time.perf_counter() - started
    (args.result_dir / "ollama_ps_after.json").write_text(
        json.dumps(_ollama(args.backend_port, "/api/ps"), indent=2) + "\n"
    )
    final_tests_hash = _sha256(args.workspace / "tests" / "test_task.py")
    (args.result_dir / "run_summary.json").write_text(
        json.dumps(
            {
                "task": "jsonl-transform",
                "source": "PR #7910",
                "source_commit": SOURCE_COMMIT,
                "fixture_sha256": FIXTURE_SHA256,
                "model": args.model,
                "provider_model": f"rugged-offline/{args.model}",
                "opencode_version": "1.19.0",
                "started_at": started_at.isoformat(),
                "wall_seconds": wall_seconds,
                "offline_profile": {
                    "network_namespace": "bubblewrap --unshare-net",
                    "backend": f"fixed loopback port {args.backend_port} through a mode-0600 temporary Unix socket bridge",
                    "sampling": "OpenCode-native agent temperature=0.2, top_p=0.95, reasoningEffort=none",
                    "local_tools": ["read", "edit", "write", "bash", "glob", "grep", "list"],
                    "delegation": "task tool denied; web, skills, and LSP denied",
                    "mcp": "no MCP servers configured",
                },
                "source_test_sha256_before": baseline_tests_hash,
                "source_test_sha256_after": final_tests_hash,
                "source_tests_unchanged": baseline_tests_hash == final_tests_hash,
                "opencode_exit_code": completed.returncode,
                "workspace_file_sha256": {
                    str(path.relative_to(args.workspace)): _sha256(path)
                    for path in sorted(args.workspace.rglob("*"))
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
                },
            },
            indent=2,
        )
        + "\n"
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
