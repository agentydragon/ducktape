#!/usr/bin/env python3
"""Verify OpenCode sends the rugged offline profile to a loopback mock server."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

RUN_DIR = Path(__file__).resolve().parent
SMOKE_DIR = RUN_DIR / "smoke_7910"
EVAL_DIR = Path(__file__).resolve().parents[5] / "x" / "local_llm" / "eval"
OPENCODE = "/nix/store/0j2gmj4zx6xac1iipzy9m3ybh6wwkh6p-opencode-1.19.0/bin/opencode"


class MockHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, object]]] = []

    def do_GET(self) -> None:
        if self.path != "/api/tags":
            self.send_error(404)
            return
        payload = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(request)
        model = request.get("model", "mock-model")
        if not request.get("stream", False):
            payload = json.dumps(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "mock-wire-ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "mock-wire-ok"}, "finish_reason": None}
                ],
            },
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, help="exercise a built rugged-opencode-offline launcher")
    parser.add_argument("--workspace", type=Path, help="writable workspace for the built launcher")
    parser.add_argument("--profile-config", type=Path, help="generated OpenCode JSON for the selected profile")
    parser.add_argument("--backend-port", type=int, help="fixed host-loopback port forwarded to the mock server")
    parser.add_argument(
        "--selector",
        choices=("qwen3-4b", "gemma4-e2b", "gemma4-e2b-long-128k-experimental", "gemma4-e4b"),
        default="qwen3-4b",
        help="local model profile passed to the built launcher",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)

    expected_models = {
        "qwen3-4b": ("qwen3:4b-instruct-2507-q4_K_M", 32768, 4096),
        "gemma4-e2b": ("gemma4:e2b-it-qat", 131072, 8192),
        "gemma4-e2b-long-128k-experimental": ("gemma4:e2b-it-qat-long-128k-b128", 131072, 4096),
        "gemma4-e4b": ("gemma4:e4b", 32768, 4096),
    }
    expected_model, expected_context, expected_output = expected_models[args.selector]
    config_checks: dict[str, object] = {}
    if args.launcher is not None:
        if args.backend_port is None or args.workspace is None or args.profile_config is None:
            parser.error("--launcher requires --backend-port, --workspace, and --profile-config")
        config = json.loads(args.profile_config.read_text())
        configured_model = config["model"]
        profile = config["provider"]["rugged-offline"]["models"][expected_model]
        configured_limits = profile["limit"]
        config_checks = {
            "config_model": configured_model,
            "expected_config_model": f"rugged-offline/{expected_model}",
            "context_limit": configured_limits.get("context"),
            "expected_context_limit": expected_context,
            "output_limit": configured_limits.get("output"),
            "expected_output_limit": expected_output,
            "tool_call": profile.get("tool_call"),
        }
        if (
            configured_model != config_checks["expected_config_model"]
            or configured_limits.get("context") != expected_context
            or configured_limits.get("output") != expected_output
            or profile.get("tool_call") is not True
        ):
            raise RuntimeError(f"generated model profile does not match selector: {config_checks}")

    mock_port = args.backend_port if args.launcher is not None else 0
    server = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture_workspace = SMOKE_DIR / "jsonl-transform"
    if args.launcher is not None:
        command = [
            str(args.launcher.resolve(strict=True)),
            "run",
            "--dir",
            "/tmp/offline-workspace",
            "--format",
            "json",
            "--title",
            "Built offline OpenCode launcher wire verification",
            "Reply exactly mock-wire-ok.",
        ]
        env = {
            **os.environ,
            "RUGGED_OPENCODE_BACKEND_PORT": str(args.backend_port),
            "RUGGED_OPENCODE_MODEL": args.selector,
        }
    else:
        command = [
            sys.executable,
            str(EVAL_DIR / "offline_exec.py"),
            "--workspace",
            str(fixture_workspace),
            "--config-dir",
            str(SMOKE_DIR / "config_home" / "opencode"),
            "--backend-port",
            str(server.server_address[1]),
            "--",
            OPENCODE,
            "run",
            "--dir",
            "/tmp/eval-work",
            "--model",
            "rugged/qwen3:4b-instruct-2507-q4_K_M",
            "--agent",
            "build",
            "--format",
            "json",
            "--title",
            "OpenCode offline profile wire verification",
            "Use the write tool to create wire-probe.txt containing 'written', then reply mock-wire-ok.",
        ]
        env = None
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=args.workspace.resolve(strict=True) if args.launcher is not None else None,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    (args.output_dir / "opencode.stdout.log").write_text(completed.stdout)
    (args.output_dir / "opencode.stderr.log").write_text(completed.stderr)
    if len(MockHandler.requests) != 1:
        raise RuntimeError(f"expected one mock completion request, got {len(MockHandler.requests)}")
    request = MockHandler.requests[0]
    (args.output_dir / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    raw_tools = request.get("tools", [])
    tools = [tool for tool in raw_tools if isinstance(tool, dict)] if isinstance(raw_tools, list) else []
    tool_names = []
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict):
            tool_names.append(function.get("name"))
    checks = {
        "exit_code": completed.returncode,
        "request_count": len(MockHandler.requests),
        "model": request.get("model"),
        "expected_model": expected_model,
        "model_matches_selector": request.get("model") == expected_model,
        "max_tokens": request.get("max_tokens"),
        "expected_max_tokens": expected_output,
        **config_checks,
        "reasoning_effort": request.get("reasoning_effort"),
        "temperature": request.get("temperature"),
        "top_p": request.get("top_p"),
        "tool_count": len(tools),
        "tool_names": tool_names,
        "response_seen": "mock-wire-ok" in completed.stdout,
    }
    (args.output_dir / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2))
    if (
        completed.returncode != 0
        or request.get("model") != expected_model
        or request.get("max_tokens") != expected_output
        or request.get("reasoning_effort") != "none"
        or request.get("temperature") != 0.2
        or request.get("top_p") != 0.95
        or not {"read", "write", "edit", "bash", "glob", "grep"}.issubset(tool_names)
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
