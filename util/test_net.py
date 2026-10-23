import errno
import socket

import pytest
import pytest_bazel

from util.net import listen_tcp, refusing_tcp_endpoint

HOST = "127.0.0.1"


def test_listener_owns_a_listening_socket_until_context_exit() -> None:
    with listen_tcp(HOST) as listener:
        assert (listener.host, listener.port) == (HOST, listener.socket.getsockname()[1])
        with (
            socket.create_connection((listener.host, listener.port), timeout=1),
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other,
            pytest.raises(OSError, match="in use") as raised,
        ):
            other.bind((HOST, listener.port))
        assert raised.value.errno == errno.EADDRINUSE


def test_refusing_endpoint_holds_a_bound_port_without_listening() -> None:
    with refusing_tcp_endpoint(HOST) as (host, port):
        with pytest.raises(OSError, match="refused") as raised:
            socket.create_connection((host, port), timeout=1)
        assert raised.value.errno == errno.ECONNREFUSED
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other,
            pytest.raises(OSError, match="in use") as raised,
        ):
            other.bind((host, port))
        assert raised.value.errno == errno.EADDRINUSE

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        other.bind((host, port))


if __name__ == "__main__":
    pytest_bazel.main()
