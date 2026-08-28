"""The pinned app-server authenticates its configured OpenAI-compatible provider from env."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest_bazel

from haku.runtime.x.bridge.codex_harness import codex_harness
from haku.runtime.x.bridge.codex_options import CodexAppServerSession, CodexModelProvider, build_codex_launch
from util.bazel.runfiles import get_required_path

_PROVIDER_SECRET = "provider-secret"


class _RequestRecorder(BaseHTTPRequestHandler):
    requests: queue.Queue[tuple[str, str | None]]

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self.requests.put((self.path, self.headers.get("authorization")))
        self.send_response(500)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"verification stop"}}')

    def log_message(self, _format: str, *args: object) -> None:
        del args


def _send(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()


def _response(process: subprocess.Popen[str], request_id: str) -> dict[str, Any]:
    assert process.stdout is not None
    while line := process.stdout.readline():
        message = json.loads(line)
        if isinstance(message, dict) and message.get("id") == request_id:
            return cast(dict[str, Any], message)
    assert process.stderr is not None
    raise AssertionError(f"Codex stopped before replying to {request_id}: {process.stderr.read()}")


def test_app_server_uses_the_configured_provider_environment_key(tmp_path: Path) -> None:
    requests: queue.Queue[tuple[str, str | None]] = queue.Queue()
    _RequestRecorder.requests = requests
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RequestRecorder)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    codex_home = tmp_path / "codex-home"
    home = tmp_path / "home"
    codex_home.mkdir()
    home.mkdir()
    provider = CodexModelProvider(
        provider_id="haku",
        name="Haku OpenAI-compatible",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key_env_var="OPENAI_API_KEY",
    )
    launch = build_codex_launch(
        CodexAppServerSession(
            cwd=tmp_path,
            environment={"CODEX_HOME": str(codex_home), "HOME": str(home), "OPENAI_API_KEY": _PROVIDER_SECRET},
            model_provider=provider,
        )
    )
    binary = get_required_path("codex_cli_linux_x64/bin/codex")
    resolved = codex_harness(binary).resolve(launch)
    process = subprocess.Popen(
        resolved.command,
        cwd=resolved.cwd,
        env=resolved.environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _send(
            process,
            {
                "method": "initialize",
                "id": "initialize",
                "params": {
                    "clientInfo": {"name": "haku-test", "title": "Haku test", "version": "0"},
                    "capabilities": None,
                },
            },
        )
        assert "result" in _response(process, "initialize")
        _send(process, {"method": "initialized"})
        _send(
            process,
            {
                "method": "thread/start",
                "id": "thread",
                "params": {
                    "cwd": str(tmp_path),
                    "model": "codex-gpt-5.6-sol",
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "ephemeral": True,
                },
            },
        )
        thread_id = _response(process, "thread")["result"]["thread"]["id"]
        _send(
            process,
            {
                "method": "turn/start",
                "id": "turn",
                "params": {"threadId": thread_id, "input": [{"type": "text", "text": "hello", "text_elements": []}]},
            },
        )
        assert "result" in _response(process, "turn")

        path, authorization = requests.get(timeout=20)
        assert path == "/v1/responses"
        assert authorization == f"Bearer {_PROVIDER_SECRET}"
        assert _PROVIDER_SECRET not in " ".join(resolved.command)
    finally:
        process.terminate()
        process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    pytest_bazel.main()
