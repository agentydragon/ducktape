"""Smoke-test the Bazel dev launcher and the assets it serves."""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


class BackendHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        response = json.dumps(
            {"path": self.path, "authorization": self.headers["Authorization"], "body": body.decode()}
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "props_session=test; HttpOnly")
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"path": self.path, "cookie": self.headers["Cookie"]}).encode())


def test_serves_built_frontend() -> None:
    binary = Runfiles.CreateOrRaise().root() / os.environ["PROPS_DEV_RLOCATION"]
    log_path = undeclared_outputs_dir() / "dev-server.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen([binary, "--frontend-only"], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"dev server exited; see {log_path}")
                try:
                    with urllib.request.urlopen("http://localhost:5173/dist/main.js", timeout=1) as response:
                        javascript = response.read()
                    with urllib.request.urlopen("http://localhost:5173/dist/main.css", timeout=1) as response:
                        css = response.read()
                    assert b"createRoot" in javascript
                    assert len(css) > 1000
                    break
                except OSError:
                    time.sleep(0.25)
            else:
                raise AssertionError(f"dev server did not serve assets; see {log_path}")

            with ThreadingHTTPServer(("127.0.0.1", 8000), BackendHandler) as backend:
                thread = threading.Thread(target=backend.serve_forever)
                thread.start()
                try:
                    request = urllib.request.Request(
                        "http://localhost:5173/api/runs/validation?split=train",
                        data=b'{"example":1}',
                        headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request) as response:
                        assert response.status == 201
                        assert response.headers["Set-Cookie"] == "props_session=test; HttpOnly"
                        assert json.load(response) == {
                            "path": "/api/runs/validation?split=train",
                            "authorization": "Bearer test",
                            "body": '{"example":1}',
                        }

                    request = urllib.request.Request(
                        "http://localhost:5173/auth/me", headers={"Cookie": "session=test"}
                    )
                    with pytest.raises(urllib.error.HTTPError) as exc:
                        urllib.request.urlopen(request)
                    assert exc.value.code == 401
                    assert json.load(exc.value) == {"path": "/auth/me", "cookie": "session=test"}
                finally:
                    backend.shutdown()
                    thread.join()

            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen("http://localhost:5173/api/stats/overview")
            assert exc.value.code == 502
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    pytest_bazel.main()
