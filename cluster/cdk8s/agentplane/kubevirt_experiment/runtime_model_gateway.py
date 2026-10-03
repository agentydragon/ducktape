"""Deterministic Anthropic/OpenAI model fixture for the disposable VM prototype.

This endpoint is intentionally not a credential gateway. It reviews the egress-sidecar
bearer on each request, then returns fixed model output without logging request data.
"""

from __future__ import annotations

import itertools
import json
import re
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TOKEN_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TOKEN_REVIEW_URL = "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
CONTEXT = ssl.create_default_context(cafile=str(TOKEN_DIR / "ca.crt"))
MAX_BODY_BYTES = 8 * 1024 * 1024
_ids = itertools.count(1)
_ids_lock = threading.Lock()
_RECOVERY_MARKER = re.compile(r"\bVM_RECOVERY_[0-9a-f]{8,64}\b")


def _next_id(prefix: str) -> str:
    with _ids_lock:
        return f"{prefix}_{next(_ids)}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def _content_texts(content: Any) -> list[str]:
    """Read text only from message content blocks; ignore tool data and other fields."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            item["text"]
            for item in content
            if isinstance(item, dict)
            and item.get("type") in {"text", "input_text", "output_text"}
            and isinstance(item.get("text"), str)
        ]
    return []


def _transcript_texts(body: dict[str, Any], role: str) -> list[str]:
    """Read one role's messages from both vendor request envelopes."""
    texts: list[str] = []
    messages = body.get("messages", [])
    if isinstance(messages, list):
        texts.extend(
            text
            for message in messages
            if isinstance(message, dict) and message.get("role") == role
            for text in _content_texts(message.get("content"))
        )
    items = body.get("input", [])
    if isinstance(items, list):
        texts.extend(
            text
            for item in items
            if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == role
            for text in _content_texts(item.get("content"))
        )
    return texts


def _marker_response(body: dict[str, Any]) -> str:
    """Save then recover the synthetic user's marker using transcript context only."""
    user_texts = _transcript_texts(body, "user")
    assistant_texts = _transcript_texts(body, "assistant")
    marker = next((match.group(0) for text in user_texts if (match := _RECOVERY_MARKER.search(text))), None)
    if marker is not None:
        if "MARKER_SAVED" in assistant_texts:
            return marker
        return "MARKER_SAVED"
    if any("recovery marker" in text.lower() for text in user_texts):
        return "VM_RECOVERY_CONTEXT_MISSING"
    return "VM_ACCEPTANCE_OK"


def _anthropic_message(body: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "id": _next_id("msg_vm_acceptance"),
        "type": "message",
        "role": "assistant",
        "model": body.get("model") if isinstance(body.get("model"), str) else "claude-3-7-sonnet-20250219",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 1,
        },
    }


def _anthropic_events(message: dict[str, Any]) -> list[bytes]:
    block = {"type": "text", "text": message["content"][0]["text"]}
    packets = [
        ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": block["text"]}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return [f"event: {event}\ndata: {_json_bytes(payload).decode()}\n\n".encode() for event, payload in packets]


def _openai_response(body: dict[str, Any], text: str) -> dict[str, Any]:
    model = body.get("model") if isinstance(body.get("model"), str) else "gpt-5-codex"
    item = {
        "id": _next_id("msg_vm_acceptance"),
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "annotations": [], "text": text}],
    }
    return {
        "id": _next_id("resp_vm_acceptance"),
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": model,
        "output": [item],
        "output_text": text,
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
    }


def _openai_events(response: dict[str, Any]) -> list[bytes]:
    item = response["output"][0]
    item_id = item["id"]
    text = response["output_text"]
    envelope = {key: response[key] for key in ("id", "object", "created_at", "model")}
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": {**envelope, "status": "in_progress", "output": []}},
        {"type": "response.in_progress", "response": {**envelope, "status": "in_progress", "output": []}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "annotations": [], "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": text},
        {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": item["content"][0],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    return [
        f"data: {_json_bytes({**event, 'sequence_number': index}).decode()}\n\n".encode()
        for index, event in enumerate(events)
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Request targets and error messages can contain user-controlled data.
        pass

    def _reply(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_connection_close()
        self.end_headers()
        self.wfile.write(data)

    def send_connection_close(self) -> None:
        self.send_header("Connection", "close")
        self.close_connection = True

    def _error(self, status: int, message: str) -> None:
        self._reply(status, "application/json", _json_bytes({"error": {"message": message, "type": "api_error"}}))

    def _review_bearer(self) -> dict[str, Any] | None:
        values = self.headers.get_all("Proxy-Authorization", [])
        if len(values) != 1:
            self._error(407, "relay token required")
            return None
        scheme, separator, token = values[0].partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or token.strip() != token:
            self._error(407, "relay token required")
            return None
        review = {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "spec": {"token": token, "audiences": ["agentplane-egress"]},
        }
        try:
            request = urllib.request.Request(
                TOKEN_REVIEW_URL,
                data=_json_bytes(review),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + (TOKEN_DIR / "token").read_text().strip(),
                },
                method="POST",
            )
            with urllib.request.urlopen(request, context=CONTEXT, timeout=5) as response:
                review_result = json.load(response)
        except OSError, ValueError, urllib.error.URLError:
            self._error(503, "token review unavailable")
            return None
        if not isinstance(review_result, dict):
            self._error(503, "token review unavailable")
            return None
        status = review_result.get("status")
        if not isinstance(status, dict):
            self._error(503, "token review unavailable")
            return None
        if not status.get("authenticated"):
            self._error(403, "token review denied")
            return None
        return status

    def _path(self) -> str:
        # A plain HTTP proxy request can arrive in absolute-form; local probes use origin-form.
        return urlsplit(self.path).path

    def do_GET(self) -> None:
        status = self._review_bearer()
        if status is None:
            return
        if self._path() == "/probe":
            user = status.get("user", {})
            data = {
                "username": user.get("username", ""),
                "pod_uid": user.get("extra", {}).get("authentication.kubernetes.io/pod-uid", []),
            }
            self._reply(200, "application/json", _json_bytes(data))
            return
        self._error(404, "unknown fixture route")

    def do_POST(self) -> None:
        if self._review_bearer() is None:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "invalid content length")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(413, "request too large")
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError, UnicodeDecodeError:
            self._error(400, "invalid JSON request")
            return
        if not isinstance(body, dict):
            self._error(400, "invalid JSON request")
            return

        path = self._path().rstrip("/")
        text = _marker_response(body)
        if path == "/v1/messages":
            message = _anthropic_message(body, text)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_connection_close()
                self.end_headers()
                try:
                    for packet in _anthropic_events(message):
                        self.wfile.write(packet)
                        self.wfile.flush()
                except BrokenPipeError, ConnectionResetError:
                    pass
            else:
                self._reply(200, "application/json", _json_bytes(message))
            return
        if path == "/v1/responses":
            response = _openai_response(body, text)
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_connection_close()
                self.end_headers()
                try:
                    for packet in _openai_events(response):
                        self.wfile.write(packet)
                        self.wfile.flush()
                except BrokenPipeError, ConnectionResetError:
                    pass
            else:
                self._reply(200, "application/json", _json_bytes(response))
            return
        self._error(404, "unknown fixture route")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8888), Handler).serve_forever()
