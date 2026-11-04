"""Generated Python gRPC through the shipped Go bridge, without a VM."""

import selectors
import socket
import socketserver
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel

from agentplane.env.firecracker import management_pb2, management_pb2_grpc
from agentplane.firecracker import guest_management
from util.bazel.runfiles import get_required_path, own_repo_rlocation


class Guest(management_pb2_grpc.GuestManagementServicer):
    def __init__(self, *, block: bool = False) -> None:
        self.requests: list[management_pb2.SynchronizeRequest] = []
        self.status_called = threading.Event()
        self.cancelled = threading.Event()
        self.block = block

    @override
    def GetStatus(
        self, request: management_pb2.GetStatusRequest, context: grpc.ServicerContext
    ) -> management_pb2.GetStatusResponse:
        context.add_callback(self.cancelled.set)
        self.status_called.set()
        if self.block:
            assert self.cancelled.wait(10), "client failed to cancel GetStatus"
        return management_pb2.GetStatusResponse(boot_id="guest-boot")

    @override
    def Synchronize(
        self, request: management_pb2.SynchronizeRequest, context: grpc.ServicerContext
    ) -> management_pb2.SynchronizeResponse:
        self.requests.append(request)
        return management_pb2.SynchronizeResponse(boot_id="guest-boot")


@contextmanager
def firecracker_transport(guest: Guest, *, reject_first: bool = False) -> Iterator[Path]:
    with ThreadPoolExecutor(max_workers=2) as workers:
        server = grpc.server(workers)
        management_pb2_grpc.add_GuestManagementServicer_to_server(guest, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        rejected = threading.Event()

        class Connection(socketserver.BaseRequestHandler):
            @override
            def handle(self) -> None:
                connection: socket.socket = self.request
                command = bytearray()
                while not command.endswith(b"\n") and len(command) < 64:
                    byte = connection.recv(1)
                    if not byte:
                        return
                    command.extend(byte)
                assert command == b"CONNECT 52\n"
                if reject_first and not rejected.is_set():
                    rejected.set()
                    connection.sendall(b"ERR booting\n")
                    return
                with socket.create_connection(("127.0.0.1", port)) as upstream, selectors.DefaultSelector() as ready:
                    connection.sendall(b"OK 52\n")
                    ready.register(connection, selectors.EVENT_READ, upstream)
                    ready.register(upstream, selectors.EVENT_READ, connection)
                    peers = {connection.fileno(): (connection, upstream), upstream.fileno(): (upstream, connection)}
                    while True:
                        for key, _ in ready.select(timeout=5):
                            source, destination = peers[key.fd]
                            data = source.recv(65536)
                            if not data:
                                return
                            destination.sendall(data)

        path = Path("firecracker.sock")
        try:
            with socketserver.ThreadingUnixStreamServer(str(path), Connection) as transport:
                thread = threading.Thread(target=transport.serve_forever)
                thread.start()
                try:
                    yield path
                finally:
                    transport.shutdown()
                    thread.join()
        finally:
            server.stop(0).wait()
            path.unlink(missing_ok=True)


@pytest.fixture
def configured_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = get_required_path(
        own_repo_rlocation("agentplane/firecracker/vsock_bridge/agentplane-firecracker-vsock-bridge")
    ).resolve()
    # Short relative names stay within Linux's Unix-socket path limit in runfiles.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(guest_management, "WORK", Path())
    monkeypatch.setattr(guest_management, "BRIDGE", bridge)
    tls = tmp_path / "tls"
    tls.mkdir()
    for name in ("tls.crt", "tls.key", "ca.crt"):
        (tls / name).write_text(name)
    monkeypatch.setattr(guest_management, "TLS_DIRECTORY", tls)


def assert_transport_cleaned() -> None:
    assert not list(Path().glob("fc-grpc-*"))


def test_generated_client_through_real_bridge(configured_launcher: None) -> None:
    guest = Guest()
    before = time.time_ns()
    with firecracker_transport(guest, reject_first=True) as path:
        assert guest_management.sync_guest(path, threading.Event(), "guest-boot") == "guest-boot"
    assert len(guest.requests) == 1
    request = guest.requests[0]
    assert request.tls_cert == "tls.crt"
    assert request.tls_key == "tls.key"
    assert request.ca_bundle == "ca.crt"
    assert request.expected_boot_id == "guest-boot"
    assert before <= request.unix_nano <= time.time_ns()
    assert_transport_cleaned()


def test_wrong_boot_never_receives_credentials(configured_launcher: None) -> None:
    guest = Guest()
    with firecracker_transport(guest) as path, pytest.raises(RuntimeError, match="differs from checkpoint"):
        guest_management.sync_guest(path, threading.Event(), "other-boot")
    assert not guest.requests
    assert_transport_cleaned()


def test_stop_cancels_rpc_and_cleans_transport(configured_launcher: None) -> None:
    guest = Guest(block=True)
    stopped = threading.Event()

    def stop() -> None:
        assert guest.status_called.wait(10)
        stopped.set()

    with ThreadPoolExecutor(max_workers=1) as workers, firecracker_transport(guest) as path:
        stopping = workers.submit(stop)
        with pytest.raises(RuntimeError, match="launcher is stopping"):
            guest_management.sync_guest(path, stopped)
        stopping.result()
    assert guest.cancelled.is_set()
    assert not guest.requests
    assert_transport_cleaned()


def test_rpc_deadline_propagates(configured_launcher: None, monkeypatch: pytest.MonkeyPatch) -> None:
    guest = Guest(block=True)
    monkeypatch.setattr(guest_management, "RPC_TIMEOUT", 2)
    with firecracker_transport(guest) as path, pytest.raises(grpc.RpcError) as error:
        guest_management.sync_guest(path, threading.Event())
    assert error.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert guest.cancelled.is_set()
    assert not guest.requests
    assert_transport_cleaned()


if __name__ == "__main__":
    pytest_bazel.main()
