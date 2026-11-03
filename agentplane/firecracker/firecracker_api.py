"""Firecracker's private, Unix-socket API and owned VMM process."""

import http.client
import json
import socket
import subprocess
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class FirecrackerApi:
    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    def request(self, method: str, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        with closing(UnixConnection(self.socket_path)) as connection:
            connection.request(
                method, route, None if method == "GET" else json.dumps(payload), {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            body = response.read()
            if response.status >= 300:
                raise RuntimeError(f"Firecracker {method} {route}: HTTP {response.status}: {body.decode()}")
            return json.loads(body) if body else {}


@contextmanager
def process(binary: Path, socket_path: Path, log_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    with (
        log_path.open("ab") as output,
        subprocess.Popen(
            [str(binary), "--api-sock", str(socket_path)], stdin=subprocess.DEVNULL, stdout=output, stderr=output
        ) as child,
    ):
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
