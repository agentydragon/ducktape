import errno
import socket

import httpx
import pytest
import pytest_bazel

from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from util.net import listen_tcp


async def test_scenarios_reuse_the_session_listener_without_rebinding() -> None:
    with listen_tcp() as listener:
        for _ in range(2):
            upstream = ScriptedUpstream()
            async with upstream.serving(listener=listener.socket), httpx.AsyncClient() as client:
                response = await client.get(f"http://{listener.host}:{listener.port}/missing")
                assert response.status_code == 404
            with socket.socket() as competitor, pytest.raises(OSError, match="Address already in use") as failure:
                competitor.bind((listener.host, listener.port))
            assert failure.value.errno == errno.EADDRINUSE


if __name__ == "__main__":
    pytest_bazel.main()
