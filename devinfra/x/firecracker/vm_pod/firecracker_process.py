"""Start a Firecracker process and expose its API.

The entrypoint is infrastructure-only: it starts the Firecracker process,
waits for the API socket, and returns a handle. All VM configuration
(boot-source, drives, machine-config, InstanceStart, snapshot/load) is
done by the manager via the FC API proxy on port 2026.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FirecrackerProcess:
    """A running Firecracker process with its API socket."""

    process: subprocess.Popen
    api_socket: Path


def start_firecracker(firecracker_bin: Path, api_socket: Path | None = None) -> FirecrackerProcess:
    """Start the Firecracker process and wait for its API socket."""
    if api_socket is None:
        api_socket = Path(tempfile.mkdtemp(prefix="firecracker-")) / "api.sock"

    logger.info("Starting Firecracker: bin=%s, socket=%s", firecracker_bin, api_socket)
    proc = subprocess.Popen(
        [firecracker_bin, "--api-sock", api_socket], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    for _ in range(50):
        if api_socket.exists():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"Firecracker API socket {api_socket} did not appear")

    logger.info("Firecracker API ready (pid=%d)", proc.pid)
    return FirecrackerProcess(process=proc, api_socket=api_socket)
