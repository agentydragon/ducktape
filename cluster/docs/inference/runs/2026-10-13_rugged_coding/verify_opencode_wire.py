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
    parser.add_argument("--backend-port", type=int, help="fixed host-loopback port forwarded to the mock server")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)

    mock_port = args.backend_port if args.launcher is not None else 0
    server = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture_workspace = SMOKE_DIR / "jsonl-transform"
    if args.launcher is not None:
        if args.backend_port is None:
            parser.error("--backend-port is required with --launcher")
        command = [
            str(args.launcher.resolve(strict=True)),
            "run",
            "--dir",
            "/tmp/offline-workspace",
            "--format",
            "json",
            "--title",
            "Built offline OpenCode launcher wire verification",
            "Use the write tool to create wire-probe.txt containing 'written', then reply mock-wire-ok.",
        ]
        env = {**os.environ, "RUGGED_OPENCODE_BACKEND_PORT": str(args.backend_port)}
    else:
        command = [
            sys.executable,
            str(RUN_DIR / "offline_exec.py"),
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
            cwd=fixture_workspace if args.launcher is not None else None,
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
    tools = request.get("tools", [])
    tool_names = [tool.get("function", {}).get("name") for tool in tools if isinstance(tool, dict)]
    checks = {
        "exit_code": completed.returncode,
        "request_count": len(MockHandler.requests),
        "model": request.get("model"),
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
        or request.get("reasoning_effort") != "none"
        or request.get("temperature") != 0.2
        or request.get("top_p") != 0.95
        or not {"read", "write", "edit", "bash", "glob", "grep"}.issubset(tool_names)
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
