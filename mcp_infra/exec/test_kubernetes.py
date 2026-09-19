"""The Kubernetes exec backend's diagnosis of a rejected `pods/exec` upgrade.

The handshake is where this fails in practice and where aiohttp says least: a bare
``WSServerHandshakeError`` reading "invalid response status" with the API server's own reason
dropped. These pin that each status still names the cause an operator can act on.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from mcp_infra.exec.kubernetes import _handshake_error


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        # 403 is the real bug: the async client execs via HTTP GET, so it needs `get pods/exec`.
        (403, "get pods/exec"),
        (401, "token was rejected"),
        (503, "container is ready"),
    ],
)
def test_handshake_error_names_likely_cause(status: int, needle: str) -> None:
    message = _handshake_error(status, "Forbidden")
    assert f"HTTP {status}" in message
    assert needle in message


if __name__ == "__main__":
    pytest_bazel.main()
