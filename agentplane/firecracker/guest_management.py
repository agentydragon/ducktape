"""Invoke the typed gRPC/vsock client without exposing a guest management TCP port."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from agentplane.env.firecracker import management_pb2
from agentplane.firecracker.credentials import read_credentials

CLIENT = Path("/usr/local/bin/agentplane-firecracker-guest-client")
TLS_DIRECTORY = Path("/run/agentplane/environment-runtime-tls")
RPC_TIMEOUT = 30


def sync_guest(path: Path, stopped: threading.Event, expected_boot_id: str = "") -> str:
    credentials = read_credentials(TLS_DIRECTORY)
    request = management_pb2.SynchronizeRequest(
        tls_cert=credentials.tls_cert,
        tls_key=credentials.tls_key,
        ca_bundle=credentials.ca_bundle,
        expected_boot_id=expected_boot_id,
    )
    deadline = time.monotonic() + RPC_TIMEOUT + 5
    with subprocess.Popen(
        [str(CLIENT), "--socket", str(path), "--timeout", f"{RPC_TIMEOUT}s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as client:
        payload: bytes | None = request.SerializeToString()
        try:
            while True:
                if stopped.is_set():
                    raise RuntimeError("launcher is stopping")
                if time.monotonic() >= deadline:
                    raise TimeoutError("guest management client exceeded its RPC deadline")
                try:
                    output, errors = client.communicate(payload, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            if client.returncode:
                raise RuntimeError(f"guest management RPC failed: {errors.decode(errors='replace')[:2048]}")
            if len(output) > 4096:
                raise ValueError("guest management response is too large")
            response = management_pb2.SynchronizeResponse.FromString(output)
            if not response.boot_id:
                raise ValueError("guest management returned no boot ID")
            if expected_boot_id and response.boot_id != expected_boot_id:
                raise RuntimeError("restored guest boot ID differs from checkpoint")
            return response.boot_id
        finally:
            if client.poll() is None:
                client.kill()
                client.communicate()
