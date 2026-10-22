import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ollama, launcher, workspace, port_arg, timeout_arg, repair_prompt = sys.argv[1:]
port = int(port_arg)
timeout_s = int(timeout_arg)
evidence = "/tmp/evidence"
server_log_path = Path(evidence) / "ollama-server.log"
with server_log_path.open("wb") as server_log:
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
    command_env = {**server_env, "RUGGED_OPENCODE_BACKEND_PORT": str(port), "OPENCODE_DISABLE_AUTOUPDATE": "1"}
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
            with server_log_path.open(encoding="utf-8", errors="replace") as source:
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
            data=json.dumps(
                {
                    "model": "qwen3:4b-instruct-2507-q4_K_M",
                    "prompt": "Reply OK.",
                    "stream": False,
                    "think": False,
                    "keep_alive": "30m",
                    "options": {"num_ctx": 32768, "num_predict": 2, "temperature": 0},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(warmup_request, timeout=300) as response:
            warmup = json.load(response)
        with server_log_path.open(encoding="utf-8", errors="replace") as source:
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
        with (Path(evidence) / "offline-proof.json").open("w", encoding="utf-8") as output:
            json.dump(proof, output, indent=2)
            output.write("\n")
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
        with (
            (Path(evidence) / "opencode.stdout.jsonl").open("w", encoding="utf-8") as stdout,
            (Path(evidence) / "opencode.stderr.log").open("w", encoding="utf-8") as stderr,
        ):
            completed = subprocess.run(
                command, check=False, timeout=timeout_s, cwd=workspace, env=command_env, stdout=stdout, stderr=stderr
            )
        proof["agent_wall_seconds"] = time.perf_counter() - started
        with (Path(evidence) / "offline-proof.json").open("w", encoding="utf-8") as output:
            json.dump(proof, output, indent=2)
            output.write("\n")
        raise SystemExit(completed.returncode)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
