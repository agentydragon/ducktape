"""The Kubernetes exec backend's diagnosis of a failed `pods/exec`.

Both ends are where a library says least. aiohttp reports a rejected handshake as a bare
``WSServerHandshakeError`` reading "invalid response status", with the API server's own reason
dropped; kubernetes_asyncio reads a command that never started as a malformed exit code. These pin
that each still names the cause an operator can act on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_bazel

from mcp_infra.exec.kubernetes import PodExecError, _exit_code, _handshake_error


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


# The Status frames the kubelet ends an exec with (ServeExec in its remotecommand server).
@pytest.mark.parametrize(
    ("frame", "exit_code"),
    [
        ({"metadata": {}, "status": "Success"}, 0),
        (
            {
                "metadata": {},
                "status": "Failure",
                "message": "command terminated with non-zero exit code: exit status 3",
                "reason": "NonZeroExitCode",
                "details": {"causes": [{"reason": "ExitCode", "message": "3"}]},
            },
            3,
        ),
    ],
)
def test_exit_code_from_status_frame(frame: dict[str, Any], exit_code: int) -> None:
    assert _exit_code(json.dumps(frame).encode()) == exit_code


def test_command_that_never_started_names_the_runtime_error() -> None:
    # An executable missing from the image: an InternalError whose one cause holds the runtime's
    # error text where a NonZeroExitCode's cause holds the code.
    error = (
        'error executing command in container: failed to exec in container: failed to start exec "test-exec": '
        'OCI runtime exec failed: exec failed: unable to start container process: exec: "/opt/test/missing": '
        "stat /opt/test/missing: no such file or directory: unknown"
    )
    frame = {
        "metadata": {},
        "status": "Failure",
        "message": f"Internal error occurred: {error}",
        "reason": "InternalError",
        "details": {"causes": [{"message": error}]},
        "code": 500,
    }
    with pytest.raises(PodExecError, match=r"could not run the command: .*/opt/test/missing: no such file"):
        _exit_code(json.dumps(frame).encode())


if __name__ == "__main__":
    pytest_bazel.main()
