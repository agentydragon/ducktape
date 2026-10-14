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
import textwrap
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
    bootstrap = textwrap.dedent(
        """\
        import json, os, socket, subprocess, sys, time, urllib.request
        ollama, launcher, task_runner, run_id, port, timeout_s, results = sys.argv[1:]
        port = int(port)
        timeout_s = int(timeout_s)
        proof_path = os.path.join(results, "offline-proof-" + run_id + ".json")
        ollama_log_path = os.path.join(results, "ollama-server-" + run_id + ".log")
        ollama_log = open(ollama_log_path, "wb")
        env = {
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
        server = subprocess.Popen([ollama, "serve"], env=env, stdout=ollama_log, stderr=subprocess.STDOUT)
        proof = {"network_namespace": "outer bubblewrap --unshare-net", "ollama_host": env["OLLAMA_HOST"],
                 "ollama_no_cloud": True, "model_cache_read_only": "/mnt", "public_egress_denied": False}
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
                with open(ollama_log_path, encoding="utf-8", errors="replace") as source:
                    lines = source.read().splitlines()
                if any("dropping integrated GPU" in line for line in lines):
                    raise RuntimeError("Ollama ignored OLLAMA_IGPU_ENABLE=1 and dropped the integrated GPU")
                vulkan_lines = [
                    line for line in lines if "inference compute" in line and "library=Vulkan" in line
                ]
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
            with open(ollama_log_path, encoding="utf-8", errors="replace") as source:
                lines = source.read().splitlines()
            layer_lines = [
                line for line in lines if "offloaded" in line and "layers to GPU" in line
            ]
            if not layer_lines:
                raise RuntimeError("Ollama warmup did not report any model layers offloaded to the GPU")
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
            with open(proof_path, "w", encoding="utf-8") as output:
                json.dump(proof, output, indent=2)
                output.write("\\n")
            command = [sys.executable, task_runner, "--run-id", run_id, "--launcher", launcher,
                       "--backend-port", str(port)]
            result = subprocess.run(command, check=False, timeout=timeout_s, env=env)
            raise SystemExit(result.returncode)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            ollama_log.close()
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
