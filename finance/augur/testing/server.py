"""Launch Augur subprocesses with isolated test state."""

import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from util.bazel.runfiles import get_required_path
from util.net import listen_tcp
from util.testing.undeclared_outputs import undeclared_outputs_dir


@contextmanager
def run_server(
    *, binary: Path, tmp_path: Path, args: Sequence[str] = (), env: Mapping[str, str] | None = None
) -> Iterator[str]:
    with (
        listen_tcp() as listener,
        (undeclared_outputs_dir() / "augur-server.log").open("w") as server_log,
        subprocess.Popen(
            [
                str(binary),
                "--listen-address",
                f"fd:{listener.socket.fileno()}",
                "--config",
                str(get_required_path("_main/finance/augur/api/testdata/config.yaml")),
                *args,
            ],
            env={
                **os.environ,
                # An empty checkout prevents smoke tests from reading workstation evidence.
                "AUGUR_EVIDENCE_DIR": str(tmp_path / "evidence"),
                "HOME": str(tmp_path / "home"),
                "MPLCONFIGDIR": str(tmp_path / "matplotlib"),
                "PYTHONUNBUFFERED": "1",
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                **(env or {}),
            },
            stdout=server_log,
            stderr=server_log,
            pass_fds=[listener.socket.fileno()],
        ) as server,
    ):
        listener.socket.close()
        origin = f"http://{listener.host}:{listener.port}"
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError(
                        f"Augur server exited early with code {server.returncode}; see {server_log.name}"
                    )
                try:
                    with urllib.request.urlopen(f"{origin}/healthz", timeout=1) as response:
                        if response.status == 200 and response.read().decode() == "ok\n":
                            break
                except OSError, urllib.error.URLError:
                    time.sleep(0.25)
            else:
                raise RuntimeError(f"Augur server did not start within 30s; see {server_log.name}")
            yield origin
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
