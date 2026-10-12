"""The test-only native pipe preserves every frame and fails readers promptly on closure."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
import pytest_bazel
from pydantic import BaseModel

from agentplane.native.testing.async_process import AsyncNativeProcess, NativeProcessEofError


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


async def test_request_matching_does_not_consume_another_native_observer(tmp_path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdin.readline(); print('{\"notice\": 1}', flush=True); print('{\"reply\": 2}', flush=True)",
    ]
    async with AsyncNativeProcess(tmp_path, command, cwd=tmp_path, environment=dict(os.environ)) as process:
        observer = process.frames()
        receipt = await process.request(Trigger(trigger="go"), matches=lambda frame: frame.get("reply") == 2)

        assert receipt.frame == {"reply": 2}
        assert receipt.sequence == 2
        assert await observer.next() == {"notice": 1}
        assert await observer.next() == {"reply": 2}


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


HUNG_TREE = """
import json
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
print(json.dumps({"ready": True}), flush=True)
time.sleep(3600)
"""


async def test_assertion_failure_reaps_process_group_and_retains_trace(tmp_path) -> None:
    process = AsyncNativeProcess(
        tmp_path, [sys.executable, "-c", HUNG_TREE], cwd=tmp_path, environment=dict(os.environ)
    )

    async def fail() -> None:
        async with process:
            assert await process.frames().next() == {"ready": True}
            raise AssertionError("probe failed")

    async with asyncio.timeout(5):
        with pytest.raises(AssertionError, match="probe failed"):
            await fail()
    assert not process.alive()
    assert process.process is not None
    assert process.process.returncode is not None
    assert process.process.returncode < 0
    assert process.stdout_frames() == [{"ready": True}]
    # The descendant inherited stdout. Reaching here also proves that the
    # reader drained to EOF rather than hanging on an abandoned descendant.


async def test_cancellation_reaps_process_group_without_swallowing_cancel(tmp_path) -> None:
    process = AsyncNativeProcess(
        tmp_path, [sys.executable, "-c", HUNG_TREE], cwd=tmp_path, environment=dict(os.environ)
    )
    ready = asyncio.Event()

    async def drive() -> None:
        async with process:
            assert await process.frames().next() == {"ready": True}
            ready.set()
            await asyncio.Event().wait()

    async with asyncio.timeout(5):
        task = asyncio.create_task(drive())
        try:
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert not process.alive()
    assert process.stdout_frames() == [{"ready": True}]


async def test_successful_exit_remains_graceful(tmp_path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('{\"ready\": true}', flush=True); sys.stdin.read(); print('{\"closed\": true}', flush=True)",
    ]
    async with AsyncNativeProcess(tmp_path, command, cwd=tmp_path, environment=dict(os.environ)) as process:
        assert await process.frames().next() == {"ready": True}
    assert process.process is not None
    assert process.process.returncode == 0
    assert process.stdout_frames() == [{"ready": True}, {"closed": True}]


if __name__ == "__main__":
    pytest_bazel.main()
