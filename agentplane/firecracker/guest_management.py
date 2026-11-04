"""Call the generated guest-management client through a private vsock transport."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import grpc

from agentplane.env.firecracker import management_pb2, management_pb2_grpc
from agentplane.firecracker.credentials import read_credentials

BRIDGE = Path("/usr/local/bin/agentplane-firecracker-vsock-bridge")
WORK = Path("/work")
TLS_DIRECTORY = Path("/run/agentplane/environment-runtime-tls")
RPC_TIMEOUT = 30
MAX_MESSAGE_BYTES = 1 << 20


def _wait_for_rpc(call: grpc.Future, stopped: threading.Event, bridge: subprocess.Popen[bytes]) -> None:
    while not call.done():
        if stopped.wait(0.1):
            raise RuntimeError("launcher is stopping")
        if bridge.poll() is not None:
            raise RuntimeError(f"guest management transport exited with status {bridge.returncode}")
    if stopped.is_set():
        raise RuntimeError("launcher is stopping")


def sync_guest(path: Path, stopped: threading.Event, expected_boot_id: str = "") -> str:
    credentials = read_credentials(TLS_DIRECTORY)
    deadline = time.monotonic() + RPC_TIMEOUT
    with tempfile.TemporaryDirectory(prefix="fc-grpc-", dir=WORK) as directory:
        socket_path = Path(directory) / "management.sock"
        with subprocess.Popen(
            [str(BRIDGE), "--socket", str(path), "--listen", str(socket_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        ) as bridge:
            try:
                with (
                    grpc.insecure_channel(
                        f"unix:{socket_path}",
                        options=(
                            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
                            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
                            ("grpc.enable_retries", 0),
                        ),
                    ) as channel,
                    ExitStack() as calls,
                ):
                    client = management_pb2_grpc.GuestManagementStub(channel)
                    status_call = client.GetStatus.future(
                        management_pb2.GetStatusRequest(),
                        timeout=max(0, deadline - time.monotonic()),
                        wait_for_ready=True,
                    )
                    calls.callback(status_call.cancel)
                    _wait_for_rpc(status_call, stopped, bridge)
                    status = status_call.result()
                    if not status.boot_id:
                        raise ValueError("guest management returned no boot ID")
                    if expected_boot_id and status.boot_id != expected_boot_id:
                        raise RuntimeError("restored guest boot ID differs from checkpoint")
                    sync_call = client.Synchronize.future(
                        management_pb2.SynchronizeRequest(
                            unix_nano=time.time_ns(),
                            tls_cert=credentials.tls_cert,
                            tls_key=credentials.tls_key,
                            ca_bundle=credentials.ca_bundle,
                            expected_boot_id=status.boot_id,
                        ),
                        timeout=max(0, deadline - time.monotonic()),
                    )
                    calls.callback(sync_call.cancel)
                    _wait_for_rpc(sync_call, stopped, bridge)
                    response = sync_call.result()
                    if response.boot_id != status.boot_id:
                        raise RuntimeError("guest boot ID changed during synchronization")
                    return response.boot_id
            finally:
                if bridge.poll() is None:
                    bridge.terminate()
                    try:
                        bridge.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        bridge.kill()
                        bridge.wait()
