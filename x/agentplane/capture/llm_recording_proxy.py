"""Header-blind local LiteLLM proxy that records bodies and preserves response streaming."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from socket import SHUT_RDWR
from threading import Lock
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from x.agentplane.capture.records import ConnectionDroppedRecord, ProxyErrorRecord, RequestRecord, ResponseChunkRecord

_SAFE_HEADERS = frozenset({"content-type", "content-encoding", "accept", "user-agent"})
_FORBIDDEN_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
Record = Callable[[RequestRecord | ResponseChunkRecord | ConnectionDroppedRecord | ProxyErrorRecord], None]


class BudgetExceededError(RuntimeError):
    """The live runner's explicit provider-call ceiling refused another request."""


def safe_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered not in _FORBIDDEN_HEADERS and lowered in _SAFE_HEADERS:
            result[lowered] = value
    return result


def _sse_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Return complete SSE packets, retaining an incomplete trailing packet."""
    frames: list[bytes] = []
    while True:
        boundaries = [
            (index, delimiter) for delimiter in (b"\r\n\r\n", b"\n\n") if (index := buffer.find(delimiter)) >= 0
        ]
        if not boundaries:
            return frames, buffer
        index, delimiter = min(boundaries, key=lambda item: item[0])
        end = index + len(delimiter)
        frames.append(buffer[:end])
        buffer = buffer[end:]


def _sse_event_name(frame: bytes) -> str | None:
    data_lines: list[bytes] = []
    for line in frame.splitlines():
        if line.startswith(b"event:"):
            return line.removeprefix(b"event:").strip().decode("ascii")
        if line.startswith(b"data:"):
            data_lines.append(line.removeprefix(b"data:").lstrip())
    if not data_lines:
        return None
    try:
        data = json.loads(b"\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return data.get("type") if isinstance(data, dict) and isinstance(data.get("type"), str) else None


def _sse_packet_matches(frame: bytes, target: str) -> bool:
    if _sse_event_name(frame) == target:
        return True
    data_lines = [line.removeprefix(b"data:").lstrip() for line in frame.splitlines() if line.startswith(b"data:")]
    try:
        data = json.loads(b"\n".join(data_lines))
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("type") == target:
        return True
    delta = data.get("delta")
    return isinstance(delta, dict) and delta.get("type") == target


def recording_proxy(
    *, upstream: str, record: Record, disconnect_after_events: tuple[str, ...] = ()
) -> ThreadingHTTPServer:
    parsed = urlsplit(upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("upstream must be an absolute HTTP(S) origin")
    request_ids = count(1)
    record_lock = Lock()
    disconnect_lock = Lock()
    remaining_disconnect_events = list(disconnect_after_events)

    def emit(event: RequestRecord | ResponseChunkRecord | ConnectionDroppedRecord | ProxyErrorRecord) -> None:
        with record_lock:
            record(event)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with record_lock:
                capture_request_id = f"llm-{next(request_ids)}"
                try:
                    record(
                        RequestRecord(
                            kind="request",
                            capture_request_id=capture_request_id,
                            method="POST",
                            path_query=self.path,
                            body=body.decode("utf-8"),
                            time_ns=time.monotonic_ns(),
                        )
                    )
                except BudgetExceededError:
                    self.send_error(429, "capture provider-call ceiling exceeded")
                    return
            disconnect_after_event = None
            # Session-title requests can repeat user text but have no native tools. Fault only an
            # actual harness turn, at the configured native-response boundary.
            is_harness_turn = bool(json.loads(body).get("tools"))
            with disconnect_lock:
                if is_harness_turn and remaining_disconnect_events:
                    disconnect_after_event = remaining_disconnect_events.pop(0)
            base_path = parsed.path.rstrip("/")
            target = (
                f"{parsed.scheme}://{parsed.netloc}{self.path}"
                if base_path and self.path.startswith(f"{base_path}/")
                else upstream.rstrip("/") + self.path
            )
            outgoing_headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "content-length"}
            }
            try:
                request = Request(target, data=body or None, headers=outgoing_headers, method="POST")
                with build_opener().open(request, timeout=120) as response:
                    self._relay(
                        capture_request_id,
                        response,
                        safe_headers(response.headers),
                        disconnect_after_event=disconnect_after_event,
                    )
            except HTTPError as error:
                self._relay_bytes(capture_request_id, error.code, error.read(), safe_headers(error.headers))
            except Exception as error:
                emit(
                    ProxyErrorRecord(
                        kind="proxy_error", capture_request_id=capture_request_id, error_kind=type(error).__name__
                    )
                )
                self.send_error(502, "recording proxy upstream failure")

        def _begin(self, status: int, headers: dict[str, str]) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            # The response is close-delimited only after upstream streaming finishes. Advertising
            # Connection: close here makes Codex abandon the stream after response.created.
            self.close_connection = True
            self.end_headers()

        def _relay(
            self, capture_request_id: str, response: Any, headers: dict[str, str], *, disconnect_after_event: str | None
        ) -> None:
            if disconnect_after_event == "response_headers":
                emit(
                    ConnectionDroppedRecord(
                        kind="connection_dropped",
                        capture_request_id=capture_request_id,
                        after_event=disconnect_after_event,
                        time_ns=time.monotonic_ns(),
                    )
                )
                with suppress(OSError):
                    self.connection.shutdown(SHUT_RDWR)
                self.connection.close()
                return
            self._begin(response.status, headers)
            ordinal = 0
            pending = b""

            def relay_frame(frame: bytes) -> bool:
                nonlocal ordinal
                ordinal += 1
                emit(
                    ResponseChunkRecord(
                        kind="response_chunk",
                        capture_request_id=capture_request_id,
                        ordinal=ordinal,
                        body=frame.decode("utf-8"),
                        time_ns=time.monotonic_ns(),
                    )
                )
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return False
                return True

            while chunk := response.read1(65536):
                pending += chunk
                frames, pending = _sse_frames(pending)
                for frame in frames:
                    if not relay_frame(frame):
                        return
                    if disconnect_after_event and _sse_packet_matches(frame, disconnect_after_event):
                        emit(
                            ConnectionDroppedRecord(
                                kind="connection_dropped",
                                capture_request_id=capture_request_id,
                                after_event=disconnect_after_event,
                                time_ns=time.monotonic_ns(),
                            )
                        )
                        with suppress(OSError):
                            self.connection.shutdown(SHUT_RDWR)
                        self.connection.close()
                        return
            if pending:
                relay_frame(pending)

        def _relay_bytes(self, capture_request_id: str, status: int, body: bytes, headers: dict[str, str]) -> None:
            self._begin(status, headers)
            emit(
                ResponseChunkRecord(
                    kind="response_chunk",
                    capture_request_id=capture_request_id,
                    ordinal=1,
                    body=body.decode("utf-8"),
                    time_ns=time.monotonic_ns(),
                )
            )
            self.wfile.write(body)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
