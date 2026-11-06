"""Smoke-test the Bazel dev launcher and the assets it serves."""

import os
import subprocess
import time
import urllib.request

import pytest_bazel
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


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
                    return
                except OSError:
                    time.sleep(0.25)
            raise AssertionError(f"dev server did not serve assets; see {log_path}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    pytest_bazel.main()
