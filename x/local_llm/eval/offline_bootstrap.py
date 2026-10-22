"""Run a command inside offline_exec's isolated network namespace."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def main() -> int:
    socat, proxy_port, backend_enabled_arg = sys.argv[1:4]
    command = sys.argv[4:]
    backend_enabled = backend_enabled_arg == "1"

    if Path("/tmp/offline-config-source").is_dir():
        shutil.copytree("/tmp/offline-config-source", "/tmp/offline-home/.config/opencode", dirs_exist_ok=True)

    relay = None
    try:
        if backend_enabled:
            relay = subprocess.Popen(
                [
                    socat,
                    f"TCP-LISTEN:{proxy_port},bind=127.0.0.1,reuseaddr,fork",
                    "UNIX-CONNECT:/tmp/offline-bridge/backend.sock",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/api/tags", timeout=2):
                        break
                except OSError:
                    if relay.poll() is not None:
                        raise RuntimeError("loopback relay exited before health check")
                    time.sleep(0.1)
            else:
                raise RuntimeError("loopback backend health check did not succeed")

        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=1):
                raise RuntimeError("offline network check failed: public egress works")
        except OSError:
            pass

        if backend_enabled:
            print("offline verified: loopback backend reachable; public egress denied", file=sys.stderr)
        else:
            print("offline verified: public egress denied; no backend bridge enabled", file=sys.stderr)

        return subprocess.run(command, check=False).returncode
    finally:
        if relay is not None:
            relay.terminate()
            try:
                relay.wait(timeout=2)
            except subprocess.TimeoutExpired:
                relay.kill()
                relay.wait()


if __name__ == "__main__":
    raise SystemExit(main())
