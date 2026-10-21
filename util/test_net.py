import errno
import socket

import pytest
import pytest_bazel

from util.net import reserve_free_port

HOST = "127.0.0.1"


def test_reserved_port_refuses_a_binder_without_reuse_address() -> None:
    port = reserve_free_port(HOST)
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other,
        pytest.raises(OSError, match="already in use") as raised,
    ):
        other.bind((HOST, port))
    assert raised.value.errno == errno.EADDRINUSE


def test_server_with_reuse_address_serves_on_reserved_port() -> None:
    port = reserve_free_port(HOST)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, port))
        server.listen()
        with socket.create_connection((HOST, port)):
            accepted, _ = server.accept()
            accepted.close()


if __name__ == "__main__":
    pytest_bazel.main()
