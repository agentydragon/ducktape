#!/usr/bin/env python3
"""Verify OpenCode sends the rugged offline profile to a loopback mock server."""

from __future__ import annotations

import argparse
import hashlib
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
MODEL_PROFILES = {
    "qwen3-4b": "qwen3:4b-instruct-2507-q4_K_M",
    "gemma4-e2b": "gemma4:e2b-it-qat",
    "gemma4-e2b-long-128k-experimental": "gemma4:e2b-it-qat-long-128k-b128",
    "gemma4-e4b": "gemma4:e4b",
    "qwen2.5-coder-7b": "qwen2.5-coder:7b",
    "qwen3-8b": "qwen3:8b",
}
EXPECTED_ROSTER = {
    "qwen3:4b-instruct-2507-q4_K_M": (32768, 4096, True, "32k"),
    "gemma4:e2b-it-qat": (32768, 8192, True, "32k"),
    "gemma4:e2b-it-qat-long-128k-b128": (131072, 4096, True, "128k"),
    "gemma4:e4b": (32768, 4096, True, "32k"),
    "qwen2.5-coder:7b": (32768, 4096, False, "32k"),
    "qwen3:8b": (32768, 4096, False, "32k"),
}


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
    parser.add_argument("--attachment", type=Path, help="attach this file to the OpenCode run")
    parser.add_argument("--expected-attachment-sha256", help="optional expected hash for the attached file")
    parser.add_argument(
        "--backend-port", type=int, default=0, help="host-loopback port; 0 chooses an available mock port"
    )
    parser.add_argument(
        "--selector",
        choices=tuple(MODEL_PROFILES),
        default="qwen3-4b",
        help="local model profile passed to the built launcher",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)

    attachment_text: str | None = None
    attachment_sha256: str | None = None
    staged_attachment: Path | None = None
    if args.attachment is not None:
        if args.launcher is None or args.workspace is None:
            parser.error("--attachment requires --launcher and --workspace")
        attachment_bytes = args.attachment.read_bytes()
        attachment_sha256 = hashlib.sha256(attachment_bytes).hexdigest()
        if args.expected_attachment_sha256 and attachment_sha256 != args.expected_attachment_sha256:
            raise ValueError(f"attached file hash mismatch: {args.attachment}")
        attachment_text = attachment_bytes.decode("utf-8")
        staged_attachment = args.workspace / ".opencode-wire-attachment.txt"
        if staged_attachment.exists():
            raise FileExistsError(f"refusing to overwrite attachment path: {staged_attachment}")
        staged_attachment.write_bytes(attachment_bytes)

    expected_model = MODEL_PROFILES[args.selector]
    expected_context, expected_output, expected_tool_call, _ = EXPECTED_ROSTER[expected_model]
    config_checks: dict[str, object] = {}
    if args.launcher is not None:
        if args.backend_port is None or args.workspace is None or args.profile_config is None:
            parser.error("--launcher requires --backend-port, --workspace, and --profile-config")
        config = json.loads(args.profile_config.read_text())
        configured_model = config["model"]
        provider = config["provider"]["rugged-offline"]
        profiles = provider["models"]
        profile = profiles[expected_model]
        configured_limits = profile["limit"]
        roster_details = {}
        roster_valid = set(profiles) == set(EXPECTED_ROSTER)
        for model_id, (context, output, tool_call, context_label) in EXPECTED_ROSTER.items():
            model_profile = profiles.get(model_id)
            if model_profile is None:
                roster_valid = False
                continue
            limits = model_profile.get("limit", {})
            display_name = model_profile.get("name", "")
            roster_details[model_id] = {
                "display_name": display_name,
                "context_limit": limits.get("context"),
                "expected_context_limit": context,
                "output_limit": limits.get("output"),
                "expected_output_limit": output,
                "tool_call": model_profile.get("tool_call"),
                "expected_tool_call": tool_call,
                "context_label": context_label,
                "context_label_matches": context_label in display_name.lower(),
            }
            roster_valid = roster_valid and (
                limits.get("context") == context
                and limits.get("output") == output
                and model_profile.get("tool_call") is tool_call
                and context_label in display_name.lower()
            )
        config_checks = {
            "config_model": configured_model,
            "expected_config_model": f"rugged-offline/{expected_model}",
            "context_limit": configured_limits.get("context"),
            "expected_context_limit": expected_context,
            "output_limit": configured_limits.get("output"),
            "expected_output_limit": expected_output,
            "tool_call": profile.get("tool_call"),
            "provider_base_url": provider["options"].get("baseURL"),
            "model_picker_roster": roster_details,
            "model_picker_roster_valid": roster_valid,
        }
        if (
            configured_model != config_checks["expected_config_model"]
            or configured_limits.get("context") != expected_context
            or configured_limits.get("output") != expected_output
            or profile.get("tool_call") is not expected_tool_call
            or provider["options"].get("baseURL") != "http://127.0.0.1:11436/v1"
            or not roster_valid
        ):
            raise RuntimeError(f"generated model profile does not match selector: {config_checks}")

    mock_port = args.backend_port if args.launcher is not None and args.backend_port > 0 else 0
    server = ThreadingHTTPServer(("127.0.0.1", mock_port), MockHandler)
    backend_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture_workspace = SMOKE_DIR / "jsonl-transform"
    if args.launcher is not None:
        command = [
            str(args.launcher.resolve(strict=True)),
            "run",
            "--dir",
            str(args.workspace.resolve(strict=True)),
            "--format",
            "json",
            "--title",
            "Built offline OpenCode launcher wire verification",
        ]
        if staged_attachment is not None:
            command.extend(["--file", staged_attachment.name, "--"])
            command.append("Use the attached text as the source. Reply exactly mock-wire-ok.")
        else:
            command.append("Reply exactly mock-wire-ok.")
        env = {**os.environ, "RUGGED_OPENCODE_BACKEND_PORT": str(backend_port), "RUGGED_OPENCODE_MODEL": args.selector}
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
        if staged_attachment is not None:
            staged_attachment.unlink(missing_ok=True)

    (args.output_dir / "opencode.stdout.log").write_text(completed.stdout)
    (args.output_dir / "opencode.stderr.log").write_text(completed.stderr)
    if len(MockHandler.requests) != 1:
        raise RuntimeError(f"expected one mock completion request, got {len(MockHandler.requests)}")
    request = MockHandler.requests[0]
    attachment_check: dict[str, object] = {}
    if attachment_text is None:
        (args.output_dir / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    else:
        messages = request.get("messages", [])

        def strings(value: object) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [item for child in value for item in strings(child)]
            if isinstance(value, dict):
                return [item for child in value.values() for item in strings(child)]
            return []

        message_strings = strings(messages)
        exact_attachment_present = any(attachment_text in value for value in message_strings)
        attachment_middle = attachment_text[len(attachment_text) // 2 - 128 : len(attachment_text) // 2 + 128]
        attachment_tail = attachment_text[-256:]
        message_summaries = []
        for message in messages if isinstance(messages, list) else []:
            values = strings(message.get("content")) if isinstance(message, dict) else []
            joined = "\n".join(values)
            message_summaries.append(
                {
                    "role": message.get("role") if isinstance(message, dict) else None,
                    "text_utf8_bytes": len(joined.encode("utf-8")),
                    "text_sha256": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
                    "attachment_prefix_256_present": attachment_text[:256] in joined,
                    "attachment_middle_256_present": attachment_middle in joined,
                    "attachment_suffix_256_present": attachment_tail in joined,
                    "tail_excerpt": joined[-240:],
                    "truncation_markers": [
                        marker
                        for marker in ("truncated", "omitted", "too large", "file limit", "Use offset")
                        if marker.lower() in joined.lower()
                    ],
                }
            )
        attachment_check = {
            "attachment_sha256": attachment_sha256,
            "attachment_utf8_bytes": len(attachment_text.encode("utf-8")),
            "attachment_exactly_present_in_outgoing_messages": exact_attachment_present,
            "outgoing_message_text_utf8_bytes": sum(len(value.encode("utf-8")) for value in message_strings),
            "raw_request_body_saved": False,
        }
        request_summary = {key: value for key, value in request.items() if key != "messages"}
        request_summary["message_count"] = len(messages) if isinstance(messages, list) else None
        request_summary["message_roles"] = (
            [message.get("role") for message in messages if isinstance(message, dict)]
            if isinstance(messages, list)
            else []
        )
        request_summary["message_summaries"] = message_summaries
        (args.output_dir / "request-summary.json").write_text(json.dumps(request_summary, indent=2) + "\n")
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
        "mock_server_host": "127.0.0.1",
        "mock_server_port": server.server_address[1],
        **attachment_check,
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
        or (attachment_text is not None and not attachment_check["attachment_exactly_present_in_outgoing_messages"])
        or (expected_tool_call and not {"read", "write", "edit", "bash", "glob", "grep"}.issubset(tool_names))
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
