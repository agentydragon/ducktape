"""The test-only native pipe preserves every frame and fails readers promptly on closure."""

from __future__ import annotations

import json
import os
import sys

import pytest
import pytest_bazel
from pydantic import BaseModel

from x.agentplane.native.async_process import AsyncNativeProcess, NativeProcessEofError


class Trigger(BaseModel):
    trigger: str


async def test_independent_cursors_observe_the_same_native_frame(tmp_path) -> None:
    command = [sys.executable, "-c", "import sys; sys.stdin.readline(); print('{\"sequence\": 1}', flush=True)"]
    async with AsyncNativeProcess(tmp_path, command, cwd=tmp_path, environment=dict(os.environ)) as process:
        first = process.frames()
        second = process.frames()
        await process.send(Trigger(trigger="go"))
        assert await first.next() == {"sequence": 1}
        assert await second.next() == {"sequence": 1}


async def test_waiting_for_a_frame_reports_stdout_eof(tmp_path) -> None:
    async with AsyncNativeProcess(
        tmp_path, [sys.executable, "-c", "pass"], cwd=tmp_path, environment=dict(os.environ)
    ) as process:
        with pytest.raises(NativeProcessEofError, match="stdout closed"):
            await process.frames().next()


async def test_waiting_for_a_frame_reports_reader_failure(tmp_path) -> None:
    command = [sys.executable, "-c", "print('not json', flush=True)"]
    with pytest.raises(json.JSONDecodeError):
        async with AsyncNativeProcess(tmp_path, command, cwd=tmp_path, environment=dict(os.environ)) as process:
            await process.frames().next()


if __name__ == "__main__":
    pytest_bazel.main()
