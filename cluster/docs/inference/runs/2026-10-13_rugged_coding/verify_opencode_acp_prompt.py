#!/usr/bin/env python3
"""Verify exact long-prompt delivery through the built OpenCode ACP launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

EXPECTED_LONG_CONTEXT = 131072
EXPECTED_OUTPUT = 4096
EXPECTED_PROMPT_SHA256 = "45c6eacad81d5bc33ff5f355a7ec901211f292237154d0d2b8ff7295f6d33c8d"
EXPECTED_MODEL = "gemma4:e2b-it-qat-long-128k-b128"


class MockHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

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


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    return []


def _send_and_wait(
    process: subprocess.Popen[str], request: dict[str, Any], events: list[str], timeout_seconds: float = 60
) -> dict[str, Any]:
    if process.stdin is None or process.stdout is None:
        raise RuntimeError("ACP subprocess pipes are unavailable")
    process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    process.stdin.flush()
    while True:
        ready, _, _ = select.select([process.stdout], [], [], timeout_seconds)
        if not ready:
            raise TimeoutError(f"timed out waiting for ACP response to request {request['id']}")
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"ACP subprocess exited before response to request {request['id']}")
        message = json.loads(line)
        if "method" in message:
            events.append(str(message["method"]))
            continue
        if message.get("id") == request["id"]:
            if "error" in message:
                raise RuntimeError(f"ACP request failed: {message['error']}")
            return cast(dict[str, Any], message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--profile-config", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--backend-port", type=int, default=0)
    parser.add_argument("--expected-prompt-sha256", default=EXPECTED_PROMPT_SHA256)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)

    prompt_bytes = args.prompt_file.read_bytes()
    prompt_text = prompt_bytes.decode("utf-8")
    prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
    if prompt_hash != args.expected_prompt_sha256:
        raise ValueError(f"frozen prompt hash mismatch: {args.prompt_file}")
    MockHandler.requests.clear()

    config = json.loads(args.profile_config.read_text())
    provider = config["provider"]["rugged-offline"]
    profile = provider["models"][EXPECTED_MODEL]
    configured_context = profile["limit"]["context"]
    configured_output = profile["limit"]["output"]
    if configured_context != EXPECTED_LONG_CONTEXT or configured_output != EXPECTED_OUTPUT:
        raise ValueError("selected profile does not match the qualified 131072 context / 4096 output limits")

    server = ThreadingHTTPServer(("127.0.0.1", args.backend_port), MockHandler)
    backend_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    command = [str(args.launcher.resolve(strict=True)), "acp", "--cwd", str(args.workspace.resolve(strict=True))]
    env = {
        **os.environ,
        "RUGGED_OPENCODE_BACKEND_PORT": str(backend_port),
        "RUGGED_OPENCODE_MODEL": "gemma4-e2b-long-128k-experimental",
    }
    events: list[str] = []
    process: subprocess.Popen[str] | None = None
    acp_result: dict[str, Any] = {}
    try:
        with (args.output_dir / "opencode.stderr.log").open("w") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=args.workspace.resolve(strict=True),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,
            )
            initialized = _send_and_wait(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "rugged-long-prompt-check", "version": "1"},
                    },
                },
                events,
            )
            session = _send_and_wait(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/new",
                    "params": {"cwd": str(args.workspace.resolve(strict=True)), "mcpServers": []},
                },
                events,
            )
            session_id = session["result"]["sessionId"]
            acp_result = _send_and_wait(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": [
                            {
                                "type": "text",
                                "text": "Check that the entire attached ledger is present. Reply mock-wire-ok.\n\n"
                                + prompt_text,
                            }
                        ],
                    },
                },
                events,
            )
            if process.stdin is None:
                raise RuntimeError("ACP stdin pipe closed before completion")
            process.stdin.close()
            process.wait(timeout=10)
            (args.output_dir / "acp.stderr-exit.json").write_text(
                json.dumps({"return_code": process.returncode}, indent=2) + "\n"
            )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    if not MockHandler.requests:
        raise RuntimeError("expected at least one mock completion request")
    request_summaries: list[dict[str, Any]] = []
    for request in MockHandler.requests:
        messages_value = request.get("messages", [])
        messages: list[dict[str, Any]] = (
            [message for message in messages_value if isinstance(message, dict)]
            if isinstance(messages_value, list)
            else []
        )
        request_strings = _strings(messages)
        request_summaries.append(
            {
                "model": request.get("model"),
                "max_tokens": request.get("max_tokens"),
                "message_count": len(messages),
                "message_roles": [message.get("role") for message in messages],
                "message_content_bytes": [
                    sum(len(value.encode("utf-8")) for value in _strings(message.get("content")))
                    for message in messages
                ],
                "message_text_utf8_bytes": sum(len(value.encode("utf-8")) for value in request_strings),
                "fixture_exactly_present": any(prompt_text in value for value in request_strings),
                "fixture_prefix_256_present": any(prompt_text[:256] in value for value in request_strings),
                "fixture_middle_256_present": any(
                    prompt_text[len(prompt_text) // 2 - 128 : len(prompt_text) // 2 + 128] in value
                    for value in request_strings
                ),
                "fixture_suffix_256_present": any(prompt_text[-256:] in value for value in request_strings),
            }
        )
    exact_prompt_request_count = sum(bool(summary["fixture_exactly_present"]) for summary in request_summaries)
    exact_prompt_present = exact_prompt_request_count > 0
    actual_models = [summary["model"] for summary in request_summaries]
    actual_model = actual_models[0]
    checks = {
        "transport": "OpenCode ACP v1 over stdio to mocked OpenAI-compatible HTTP backend",
        "exit_code": process.returncode if process is not None else None,
        "initialize_protocol_version": initialized.get("result", {}).get("protocolVersion"),
        "acp_prompt_result": acp_result.get("result"),
        "acp_mock_usage_is_synthetic": True,
        "acp_mock_usage_note": "The mock returns fixed 1-input/1-output counts; they are not prompt tokenization evidence.",
        "mock_request_count": len(MockHandler.requests),
        "requests_containing_exact_fixture": exact_prompt_request_count,
        "model": actual_model,
        "expected_model": EXPECTED_MODEL,
        "all_requests_use_selected_model": all(model == EXPECTED_MODEL for model in actual_models),
        "configured_context_limit": configured_context,
        "expected_context_limit": EXPECTED_LONG_CONTEXT,
        "configured_output_limit": configured_output,
        "request_max_tokens_by_request": [summary["max_tokens"] for summary in request_summaries],
        "expected_output_limit": EXPECTED_OUTPUT,
        "fixture_sha256": prompt_hash,
        "fixture_utf8_bytes": len(prompt_bytes),
        "fixture_tokens_per_gemma4_tokenizer": 123997,
        "fixture_exactly_present_in_outgoing_messages": exact_prompt_present,
        "fixture_prefix_256_present": any(bool(summary["fixture_prefix_256_present"]) for summary in request_summaries),
        "fixture_middle_256_present": any(bool(summary["fixture_middle_256_present"]) for summary in request_summaries),
        "fixture_suffix_256_present": any(bool(summary["fixture_suffix_256_present"]) for summary in request_summaries),
        "outgoing_message_text_utf8_bytes_by_request": [
            summary["message_text_utf8_bytes"] for summary in request_summaries
        ],
        "acp_events": events,
        "mock_server_host": "127.0.0.1",
        "mock_server_port": backend_port,
        "raw_request_body_saved": False,
        "prompt_token_count_scope": "fixture only; mock does not tokenize or measure total model input",
    }
    (args.output_dir / "request-summary.json").write_text(json.dumps(request_summaries, indent=2) + "\n")
    (args.output_dir / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2))
    if (
        process is None
        or process.returncode != 0
        or not checks["all_requests_use_selected_model"]
        or any(request.get("max_tokens") != EXPECTED_OUTPUT for request in MockHandler.requests)
        or not exact_prompt_present
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
