"""Continue a failed coding task in the same workspace with frozen verifier feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
RESULTS_DIR = RUN_DIR / "results"
SOURCE_RUN_ID = "qwen3-4b-offline-jsonl-4"
MODEL_ID = "qwen3:4b-instruct-2507-q4_K_M"
DEFAULT_LAUNCHER = Path(
    "/nix/store/hbl5rzd74qs9z679kjq7wsj6x2lcpmd9-rugged-opencode-offline/bin/rugged-opencode-offline"
)
REPAIR_PROMPT = """Continue the implementation in the current workspace from the exact verifier feedback below.

The previous implementation failed two tests in the original frozen verifier:

1. `test_normalizes_records_and_preserves_unicode`: output escaped non-ASCII characters (`José`, `東京`, and `🚀`) instead of emitting literal Unicode.
2. `test_rejects_non_string_tags` with `tags='"blue"'`: a string was accepted where the input contract requires `tags` to be a list containing only strings.

Do not modify any tests. Inspect the implementation and make the smallest correct fix. Run the full original command `python3 -m unittest -v tests.test_task`; if any test fails, fix the implementation and run the full command again. Do not report that tests pass unless you have a successful full test output. Preserve other object fields and the original task's line-numbered ValueError requirements.
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="repair-1")
    parser.add_argument("--source-run-id", default=SOURCE_RUN_ID)
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    parser.add_argument("--backend-port", type=int, default=11500)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    if not 1024 <= args.backend_port <= 65535 or args.timeout_seconds < 1:
        parser.error("backend port must be unprivileged and timeout must be positive")

    source_run = RESULTS_DIR / args.source_run_id
    workspace = source_run / "workspace"
    if not workspace.is_dir():
        parser.error(f"source workspace does not exist: {workspace}")
    original_summary = json.loads((source_run / "run_summary.json").read_text())
    original_wall_seconds = float(original_summary["wall_seconds"])
    launcher = args.launcher.resolve(strict=True)
    result_dir = source_run / args.run_id
    if result_dir.exists():
        parser.error(f"repair result directory already exists: {result_dir}")
    result_dir.mkdir(parents=True)
    shutil.copytree(workspace, result_dir / "workspace_before")
    shutil.copy2(workspace / "jsonl_transform.py", result_dir / "pre_repair_jsonl_transform.py")

    test_file = workspace / "tests" / "test_task.py"
    implementation = workspace / "jsonl_transform.py"
    before = {"test_sha256": _sha256(test_file), "implementation_sha256": _sha256(implementation)}
    prompt_sha256 = hashlib.sha256(REPAIR_PROMPT.encode()).hexdigest()
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    command = [
        "env",
        "RUGGED_OPENCODE_BACKEND_PORT=11437",
        str(launcher),
        "run",
        "--dir",
        "/tmp/offline-workspace",
        "--auto",
        "--format",
        "json",
        "--title",
        "Rugged offline JSONL transform repair continuation",
        REPAIR_PROMPT,
    ]
    offline_exec = RUN_DIR.parent / "offline_exec.py"
    wrapped_command = [
        "python3",
        str(offline_exec),
        "--workspace",
        str(workspace),
        "--backend-port",
        str(args.backend_port),
        "--",
        *command,
    ]
    completed = subprocess.run(
        wrapped_command, check=False, timeout=args.timeout_seconds, capture_output=True, text=True
    )
    wall_seconds = time.perf_counter() - started
    (result_dir / "offline-wrapper.stdout.log").write_text(completed.stdout)
    (result_dir / "offline-wrapper.stderr.log").write_text(completed.stderr)
    after = {"test_sha256": _sha256(test_file), "implementation_sha256": _sha256(implementation)}
    summary = {
        "source_run_id": args.source_run_id,
        "repair_run_id": args.run_id,
        "task": "jsonl-transform",
        "model": MODEL_ID,
        "started_at": started_at.isoformat(),
        "wall_seconds": wall_seconds,
        "original_agent_wall_seconds": original_wall_seconds,
        "cumulative_agent_wall_seconds": original_wall_seconds + wall_seconds,
        "agent_wall_time_includes_model_startup": False,
        "prompt_sha256": prompt_sha256,
        "workspace_path": str(workspace.resolve()),
        "workspace_snapshot_before": "workspace_before/",
        "network_isolation": "offline_exec.py bubblewrap --unshare-net with only the fixed loopback Ollama endpoint bridged",
        "backend_port": args.backend_port,
        "launcher_exit_code": completed.returncode,
        "test_sha256_before": before["test_sha256"],
        "test_sha256_after": after["test_sha256"],
        "tests_unchanged": before["test_sha256"] == after["test_sha256"],
        "implementation_sha256_before": before["implementation_sha256"],
        "implementation_sha256_after": after["implementation_sha256"],
        "implementation_changed": before["implementation_sha256"] != after["implementation_sha256"],
        "independent_verification": "pending Bazel verifier",
    }
    (result_dir / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
