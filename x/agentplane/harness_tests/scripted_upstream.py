"""Loopback model endpoint that a test drives one request at a time.

The harness under test POSTs to this server; the test takes each request with
`next_request`, inspects it, and answers it with `respond`. A request nobody answers
holds its connection open, so a test that forgets a step times out on the native
side rather than passing vacuously. Standing rules (`always`) answer requests
without a script step, for behavior that repeats under the harness's own control
(retry storms).
"""

from __future__ import annotations

import enum
import json
import queue
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import cached_property
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class Packet:
    """One complete SSE packet; `kind` is the name a test truncates a stream after."""

    kind: str
    body: bytes


class Close(enum.Enum):
    END = "end"  # complete response
    TRUNCATE = "truncate"  # abrupt socket shutdown after the last packet: a lost connection
    HOLD = "hold"  # keep the response open; the test continues it with another `respond`


@dataclass(frozen=True)
class Stream:
    packets: tuple[Packet, ...]
    close: Close = Close.END

    def until(self, kind: str) -> Stream:
        """Packets through the first one of `kind`, then the connection is lost."""
        index = next(index for index, packet in enumerate(self.packets) if packet.kind == kind)
        return Stream(self.packets[: index + 1], close=Close.TRUNCATE)

    def held(self) -> Stream:
        return Stream(self.packets, close=Close.HOLD)


@dataclass(frozen=True)
class Body:
    content: bytes
    status: int = 200
    content_type: str = "application/json"


@dataclass(frozen=True)
class Refuse:
    """Close the connection before any response bytes."""


Reply = Stream | Body | Refuse


@dataclass
class UpstreamRequest:
    path: str
    body: bytes
    client_closed: threading.Event = field(default_factory=threading.Event, repr=False)
    _replies: queue.Queue[Reply] = field(default_factory=queue.Queue, repr=False)

    @cached_property
    def json(self) -> Any:
        return json.loads(self.body)


Rule = Callable[[UpstreamRequest], Reply | None]


class ScriptedUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.observed: list[UpstreamRequest] = []
        self._pending: queue.Queue[UpstreamRequest] = queue.Queue()
        self._rules: list[Rule] = []
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def next_request(self, *, timeout: float = 30) -> UpstreamRequest:
        """The next request no standing rule answered; blocks until the harness sends one."""
        try:
            return self._pending.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("the harness sent no upstream request") from None

    def respond(self, request: UpstreamRequest, reply: Reply) -> None:
        request._replies.put(reply)

    def always(self, rule: Rule) -> None:
        """Answer every request the rule accepts without a script step."""
        with self._lock:
            self._rules.append(rule)

    def clear_rules(self) -> None:
        with self._lock:
            self._rules.clear()

    def assert_quiescent(self) -> None:
        """No request is waiting for a script step."""
        if not self._pending.empty():
            raise AssertionError(f"unanswered upstream requests: {list(self._pending.queue)}")

    def _admit(self, request: UpstreamRequest) -> None:
        with self._lock:
            self.observed.append(request)
            rules = list(self._rules)
        for rule in rules:
            if (reply := rule(request)) is not None:
                request._replies.put(reply)
                return
        self._pending.put(request)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ScriptedUpstream

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        request = UpstreamRequest(self.path, self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.server._admit(request)
        self.close_connection = True
        headers_sent = False
        while True:
            reply = self._await_reply(request)
            if reply is None:
                return
            if isinstance(reply, Refuse):
                self._shutdown()
                return
            if isinstance(reply, Body):
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(reply.content)))
                self.end_headers()
                self.wfile.write(reply.content)
                return
            if not headers_sent:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if reply.close is Close.END:
                    self.send_header("Content-Length", str(sum(len(packet.body) for packet in reply.packets)))
                else:
                    self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
            for packet in reply.packets:
                self.wfile.write(packet.body)
                self.wfile.flush()
            if reply.close is Close.TRUNCATE:
                self._shutdown()
            if reply.close is not Close.HOLD:
                return

    def _await_reply(self, request: UpstreamRequest) -> Reply | None:
        """The next scripted reply, or None once the client abandoned the request."""
        self.connection.setblocking(False)
        try:
            while True:
                try:
                    return request._replies.get(timeout=0.05)
                except queue.Empty:
                    pass
                try:
                    if self.connection.recv(1, socket.MSG_PEEK) == b"":
                        request.client_closed.set()
                        return None
                except BlockingIOError:
                    continue
                except OSError:
                    request.client_closed.set()
                    return None
        finally:
            self.connection.setblocking(True)

    def _shutdown(self) -> None:
        with suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()
