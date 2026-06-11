"""Tests for util.crane.

Focused on `_format_crane_error` and the `Crane._run` / `_arun` failure paths,
since those are the bits that determine how a failed crane subprocess shows
up in tracebacks. The original code wrapped the subprocess in
`subprocess.run(check=True, capture_output=True)` and let the resulting
`CalledProcessError` propagate, which only renders as the exit code — losing
the actual error message. These tests pin the new behavior: stderr (and
stdout, when present) are visible in the raised exception's message.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from util.crane import Crane, _format_crane_error


def test_format_crane_error_includes_stderr_and_stdout() -> None:
    msg = _format_crane_error(("push", "img", "repo:tag"), 1, "DENIED: insufficient_scope\n", "uploading layer\n")
    assert "crane push img repo:tag failed (exit 1)" in msg
    assert "DENIED: insufficient_scope" in msg
    assert "uploading layer" in msg


def test_format_crane_error_omits_empty_streams() -> None:
    msg = _format_crane_error(("digest", "x"), 2, "", "")
    assert msg == "crane digest x failed (exit 2)"
    assert "stderr" not in msg
    assert "stdout" not in msg


def test_format_crane_error_includes_only_nonempty_stream() -> None:
    msg = _format_crane_error(("ls", "ghcr.io/foo"), 1, "  \t\n", "tag1\ntag2\n")
    assert "stderr" not in msg
    assert "stdout:\ntag1\ntag2" in msg


def test_run_wraps_called_process_error_with_full_context() -> None:
    """Sync `_run` must re-raise as RuntimeError whose message contains stderr/stdout.

    Crucial: the previous behavior was a bare CalledProcessError that hides
    the captured streams in `__str__`, so trying to debug `crane push` failures
    from a Bazel `bb run` log gave you only `exit status 1`.
    """
    crane = Crane(path=Path("/nonexistent/crane"))

    fake_error = subprocess.CalledProcessError(
        returncode=1,
        cmd=["/nonexistent/crane", "push", "img", "ref"],
        output="partial-stdout\n",
        stderr="DENIED: requested access to the resource is denied\n",
    )

    with patch("subprocess.run", side_effect=fake_error), pytest.raises(RuntimeError) as exc_info:
        crane._run("push", "img", "ref")

    msg = str(exc_info.value)
    assert "crane push img ref failed (exit 1)" in msg
    assert "DENIED: requested access to the resource is denied" in msg
    assert "partial-stdout" in msg
    # The original CalledProcessError should be chained for tracebacks.
    assert isinstance(exc_info.value.__cause__, subprocess.CalledProcessError)


def test_run_handles_calledprocesserror_with_none_streams() -> None:
    """`subprocess.CalledProcessError` may carry `stderr=None` if capture wasn't requested."""
    crane = Crane(path=Path("/nonexistent/crane"))
    fake_error = subprocess.CalledProcessError(returncode=1, cmd=["crane", "tag", "x", "y"])
    with patch("subprocess.run", side_effect=fake_error), pytest.raises(RuntimeError) as exc_info:
        crane._run("tag", "x", "y")
    assert "crane tag x y failed (exit 1)" in str(exc_info.value)


def test_arun_raises_with_stderr_and_stdout() -> None:
    """Async `_arun` is symmetric: stderr is in the message, stdout when nonempty."""
    crane = Crane(path=Path("/nonexistent/crane"))

    class _FakeProc:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"some output\n", b"AUTH_FAILED: token expired\n")

    async def _fake_create(*args: object, **kwargs: object) -> _FakeProc:
        return _FakeProc()

    async def _go() -> None:
        with patch("asyncio.create_subprocess_exec", side_effect=_fake_create):
            await crane._arun("push", "img", "registry/foo:bar")

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(_go())

    msg = str(exc_info.value)
    assert "crane push img registry/foo:bar failed (exit 1)" in msg
    assert "AUTH_FAILED: token expired" in msg
    assert "some output" in msg


if __name__ == "__main__":
    pytest_bazel.main()
