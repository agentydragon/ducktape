"""Continue a failed coding task in the same workspace with frozen verifier feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
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
DEFAULT_OLLAMA = Path("/nix/store/845p22skhnydsa22k9c7fq5sgi22pxli-ollama-0.40.2/bin/ollama")
DEFAULT_BWRAP = Path("/nix/store/bs0izr20qsssmygsg7qzis680ncifdil-bubblewrap-0.12.0/bin/bwrap")
DEFAULT_MODEL_CACHE = Path("/tmp/ducktape-rugged-models")
REPAIR_PROMPT = """Continue the implementation in the current workspace from the exact verifier feedback below.

The previous implementation failed two tests in the original frozen verifier:

1. `test_normalizes_records_and_preserves_unicode`: output escaped non-ASCII characters (`José`, `東京`, and `🚀`) instead of emitting literal Unicode.
2. `test_rejects_non_string_tags` with `tags='"blue"'`: a string was accepted where the input contract requires `tags` to be a list containing only strings.

Do not modify any tests. Inspect the implementation and make the smallest correct fix. Run the full original command `python3 -m unittest -v tests.test_task`; if any test fails, fix the implementation and run the full command again. Do not report that tests pass unless you have a successful full test output. Preserve other object fields and the original task's line-numbered ValueError requirements.
"""
TOOL_FIRST_PROMPT = """The previous implementation still failed two tests in the original verifier:

1. `test_normalizes_records_and_preserves_unicode`: emit literal Unicode (`José`, `東京`, `🚀`) instead of JSON escape sequences.
2. `test_rejects_non_string_tags` with `tags='"blue"'`: reject a scalar string when `tags` must be a list containing only strings.

The coding tools are available and enabled. First use the `read` tool to inspect `jsonl_transform.py` and `tests/test_task.py` in this workspace. Then use the `edit` or `write` tool to fix only the implementation. Run `python3 -m unittest -v tests.test_task` through the shell tool. Continue editing and rerunning that full command until all four tests pass. Do not modify tests. Do not claim you cannot access files or tools; report any actual tool error verbatim.
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="repair-1")
    parser.add_argument("--source-run-id", default=SOURCE_RUN_ID)
    parser.add_argument("--launcher", type=Path, default=DEFAULT_LAUNCHER)
    parser.add_argument("--ollama", type=Path, default=DEFAULT_OLLAMA)
    parser.add_argument("--bwrap", type=Path, default=DEFAULT_BWRAP)
    parser.add_argument("--model-cache", type=Path, default=DEFAULT_MODEL_CACHE)
    parser.add_argument("--backend-port", type=int, default=11501)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--tool-first", action="store_true", help="explicitly require read/edit/test tool use")
    args = parser.parse_args()
    if not 1024 <= args.backend_port <= 65535 or args.timeout_seconds < 1:
        parser.error("backend port must be unprivileged and timeout must be positive")

    source_run = RESULTS_DIR / args.source_run_id
    workspace = source_run / "workspace"
    if not workspace.is_dir():
        parser.error(f"source workspace does not exist: {workspace}")
    original_summary = json.loads((source_run / "run_summary.json").read_text())
    original_wall_seconds = float(original_summary["wall_seconds"])
    for name in ("launcher", "ollama", "bwrap", "model_cache"):
        value = getattr(args, name).resolve(strict=True)
        if name == "model_cache" and not value.is_dir():
            parser.error("--model-cache must name a directory")
        if name != "model_cache" and not value.is_file():
            parser.error(f"--{name} must name a file")
        setattr(args, name, value)
    launcher = args.launcher
    result_dir = source_run / args.run_id
    if result_dir.exists():
        parser.error(f"repair result directory already exists: {result_dir}")
    result_dir.mkdir(parents=True)
    test_file = workspace / "tests" / "test_task.py"
    implementation = workspace / "jsonl_transform.py"
    before = {"test_sha256": _sha256(test_file), "implementation_sha256": _sha256(implementation)}
    before_snapshot = result_dir / "workspace_before"
    (before_snapshot / "tests").mkdir(parents=True)
    (before_snapshot / "jsonl_transform.py.txt").write_bytes(implementation.read_bytes())
    (before_snapshot / "tests" / "test_task.py.txt").write_bytes(test_file.read_bytes())
    (before_snapshot / "tests" / "__init__.py.txt").write_bytes((workspace / "tests" / "__init__.py").read_bytes())
    if _sha256(before_snapshot / "jsonl_transform.py.txt") != before["implementation_sha256"]:
        raise RuntimeError("pre-repair source archive did not preserve implementation bytes")
    if _sha256(before_snapshot / "tests" / "test_task.py.txt") != before["test_sha256"]:
        raise RuntimeError("pre-repair source archive did not preserve verifier bytes")
    repair_prompt = TOOL_FIRST_PROMPT if args.tool_first else REPAIR_PROMPT
    prompt_sha256 = hashlib.sha256(repair_prompt.encode()).hexdigest()
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    bootstrap = textwrap.dedent(
        """\
        import json, os, socket, subprocess, sys, time, urllib.request
        ollama, launcher, workspace, port, timeout_s, repair_prompt = sys.argv[1:]
        port = int(port)
        timeout_s = int(timeout_s)
        evidence = "/tmp/evidence"
        server_log_path = os.path.join(evidence, "ollama-server.log")
        server_log = open(server_log_path, "wb")
        server_env = {
            "PATH": os.environ["PATH"],
            "HOME": "/tmp/offline-home",
            "OLLAMA_HOST": f"127.0.0.1:{port}",
            "OLLAMA_MODELS": "/mnt",
            "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_CONTEXT_LENGTH": "32768",
            "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_IGPU_ENABLE": "1",
            "OLLAMA_VULKAN": "1",
        }
        server = subprocess.Popen([ollama, "serve"], env=server_env, stdout=server_log, stderr=subprocess.STDOUT)
        proof = {
            "network_namespace": "outer bubblewrap --unshare-net contains both Ollama and OpenCode",
            "ollama_host": server_env["OLLAMA_HOST"],
            "ollama_no_cloud": True,
            "model_cache_read_only": "/mnt",
            "public_egress_denied": False,
        }
        command_env = {
            **server_env,
            "RUGGED_OPENCODE_BACKEND_PORT": str(port),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
        try:
            tags = None
            for _ in range(120):
                if server.poll() is not None:
                    raise RuntimeError("isolated Ollama exited before /api/tags became ready")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tags", timeout=2) as response:
                        tags = json.load(response)
                    break
                except OSError:
                    time.sleep(0.5)
            if tags is None:
                raise RuntimeError("isolated Ollama did not answer /api/tags")
            proof["ollama_tags"] = tags
            vulkan_lines = []
            for _ in range(60):
                with open(server_log_path, encoding="utf-8", errors="replace") as source:
                    lines = source.read().splitlines()
                if any("dropping integrated GPU" in line for line in lines):
                    raise RuntimeError("Ollama ignored OLLAMA_IGPU_ENABLE=1 and dropped the integrated GPU")
                vulkan_lines = [line for line in lines if "inference compute" in line and "library=Vulkan" in line]
                if vulkan_lines:
                    break
                time.sleep(0.5)
            if not vulkan_lines:
                raise RuntimeError("Ollama did not report Vulkan as an inference compute device")
            warmup_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/generate",
                data=json.dumps({
                    "model": "qwen3:4b-instruct-2507-q4_K_M",
                    "prompt": "Reply OK.",
                    "stream": False,
                    "think": False,
                    "keep_alive": "30m",
                    "options": {"num_ctx": 32768, "num_predict": 2, "temperature": 0},
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(warmup_request, timeout=300) as response:
                warmup = json.load(response)
            with open(server_log_path, encoding="utf-8", errors="replace") as source:
                lines = source.read().splitlines()
            layer_lines = [line for line in lines if "offloaded" in line and "layers to GPU" in line]
            if not layer_lines:
                raise RuntimeError("Ollama warmup did not report model layers offloaded to the GPU")
            proof["vulkan_device_evidence"] = vulkan_lines
            proof["gpu_layer_offload_evidence"] = layer_lines
            proof["gpu_warmup"] = {
                key: warmup.get(key)
                for key in ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration", "total_duration")
            }
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ps", timeout=5) as response:
                proof["ollama_ps_after_warmup"] = json.load(response)
            try:
                with socket.create_connection(("1.1.1.1", 443), timeout=1):
                    raise RuntimeError("outer namespace unexpectedly reached public egress")
            except OSError as error:
                proof["public_egress_denied"] = True
                proof["egress_probe_error"] = f"{type(error).__name__}: {error}"
            with open(os.path.join(evidence, "offline-proof.json"), "w", encoding="utf-8") as output:
                json.dump(proof, output, indent=2)
                output.write("\\n")
            command = [
                launcher,
                "run",
                "--auto",
                "--format",
                "json",
                "--title",
                "Rugged offline JSONL transform repair continuation",
                repair_prompt,
            ]
            started = time.perf_counter()
            with open(os.path.join(evidence, "opencode.stdout.jsonl"), "w", encoding="utf-8") as stdout, \\
                 open(os.path.join(evidence, "opencode.stderr.log"), "w", encoding="utf-8") as stderr:
                completed = subprocess.run(
                    command, check=False, timeout=timeout_s, cwd=workspace, env=command_env,
                    stdout=stdout, stderr=stderr,
                )
            proof["agent_wall_seconds"] = time.perf_counter() - started
            with open(os.path.join(evidence, "offline-proof.json"), "w", encoding="utf-8") as output:
                json.dump(proof, output, indent=2)
                output.write("\\n")
            raise SystemExit(completed.returncode)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            server_log.close()
        """
    )
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
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/root",
        "--dir",
        "/tmp/offline-home",
        "--dir",
        "/tmp/evidence",
        "--bind",
        str(result_dir),
        "/tmp/evidence",
        "--ro-bind",
        str(args.model_cache),
        "/mnt",
        "--bind",
        str(workspace),
        "/var/tmp",
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
        str(launcher),
        "/var/tmp",
        str(args.backend_port),
        str(args.timeout_seconds),
        repair_prompt,
    ]
    completed = subprocess.run(command, check=False, timeout=args.timeout_seconds + 900, capture_output=True, text=True)
    outer_wall_seconds = time.perf_counter() - started
    (result_dir / "outer-namespace.stdout.log").write_text(completed.stdout)
    (result_dir / "outer-namespace.stderr.log").write_text(completed.stderr)
    proof_path = result_dir / "offline-proof.json"
    proof = json.loads(proof_path.read_text()) if proof_path.is_file() else {}
    agent_wall_seconds = proof.get("agent_wall_seconds")
    after = {"test_sha256": _sha256(test_file), "implementation_sha256": _sha256(implementation)}
    after_snapshot = result_dir / "workspace_after"
    (after_snapshot / "tests").mkdir(parents=True)
    (after_snapshot / "jsonl_transform.py.txt").write_bytes(implementation.read_bytes())
    (after_snapshot / "tests" / "test_task.py.txt").write_bytes(test_file.read_bytes())
    (after_snapshot / "tests" / "__init__.py.txt").write_bytes((workspace / "tests" / "__init__.py").read_bytes())
    if _sha256(after_snapshot / "jsonl_transform.py.txt") != after["implementation_sha256"]:
        raise RuntimeError("post-repair source archive did not preserve implementation bytes")
    if _sha256(after_snapshot / "tests" / "test_task.py.txt") != after["test_sha256"]:
        raise RuntimeError("post-repair source archive did not preserve verifier bytes")
    summary = {
        "source_run_id": args.source_run_id,
        "repair_run_id": args.run_id,
        "task": "jsonl-transform",
        "model": MODEL_ID,
        "started_at": started_at.isoformat(),
        "outer_wall_seconds": outer_wall_seconds,
        "repair_agent_wall_seconds": agent_wall_seconds,
        "original_agent_wall_seconds": original_wall_seconds,
        "initial_plus_this_attempt_agent_wall_seconds": (
            original_wall_seconds + float(agent_wall_seconds) if agent_wall_seconds is not None else None
        ),
        "initial_plus_this_attempt_excludes_prior_continuations": True,
        "time_interpretation": "This sum includes only the initial attempt and this continuation; it excludes earlier continuations and is not total resolution time.",
        "agent_wall_time_includes_model_startup": False,
        "prompt_sha256": prompt_sha256,
        "prompt_mode": "explicit_tool_first" if args.tool_first else "verifier_feedback",
        "workspace_path": str(workspace.resolve()),
        "workspace_snapshot_before": "workspace_before/*.py.txt",
        "workspace_snapshot_after": "workspace_after/*.py.txt",
        "network_isolation": "outer bubblewrap --unshare-net contains Ollama and OpenCode; OpenCode nested namespace bridges only its inner loopback to outer Ollama",
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
