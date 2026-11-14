"""Guest cleanup rejects live listeners while allowing closed TCP connections."""

import errno
import socket
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.x.direct_vm_boot.runtime_guest import (
    assert_loopback_listener_closed,
    assert_loopback_listener_free,
    gateway_address,
)
from util.net import listen_tcp


def test_guest_cleanup_distinguishes_a_live_listener_from_time_wait() -> None:
    address = gateway_address(str(uuid4()))
    with listen_tcp(address, port=9090) as listener:
        with pytest.raises(AssertionError, match="still accepts connections"):
            assert_loopback_listener_closed(address)
        # Drain the live-listener negative control before opening the connection
        # whose server side closes first, leaving TIME_WAIT on the guest port.
        accepted, _ = listener.socket.accept()
        accepted.close()
        with socket.create_connection((address, 9090), timeout=2) as client:
            accepted, _ = listener.socket.accept()
            with accepted:
                accepted.settimeout(2)
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert accepted.recv(1) == b""
    with pytest.raises(OSError, match="Address already in use") as busy:
        assert_loopback_listener_free(address)
    assert busy.value.errno == errno.EADDRINUSE
    assert_loopback_listener_closed(address)


if __name__ == "__main__":
    pytest_bazel.main()
