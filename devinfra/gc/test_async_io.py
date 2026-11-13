import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from devinfra.gc.async_io import run_blocking, run_process


async def test_repeated_cancellation_waits_for_owned_thread() -> None:
    started, release = threading.Event(), threading.Event()

    def blocking() -> None:
        started.set()
        assert release.wait(10)

    task = asyncio.create_task(run_blocking(blocking))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


@pytest.mark.parametrize("parent_exits", [False, True])
async def test_cancellation_kills_descendant_holding_output_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_exits: bool
) -> None:
    fifo = tmp_path / "started"
    os.mkfifo(fifo)
    script = tmp_path / "child.sh"
    script.write_text('#!/bin/sh\nsleep 60 &\nprintf "ready\\n" > "$1"\n' + ("" if parent_exits else "wait\n"))
    script.chmod(0o755)
    create = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def monitored(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", monitored)
    task = asyncio.create_task(run_process(str(script), str(fifo)))
    try:
        async with asyncio.timeout(10):
            assert await asyncio.to_thread(lambda: fifo.open().readline()) == "ready\n"
            if parent_exits:
                # Process.wait() also waits for the descendant-held pipes to close.
                while processes[0].returncode is None:  # noqa: ASYNC110
                    await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_process_creation_still_delivers_stdin_to_noninterruptible_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "stdin"
    script = tmp_path / "read-stdin.sh"
    script.write_text('#!/bin/sh\ncat > "$1"\n')
    script.chmod(0o755)
    create = asyncio.create_subprocess_exec
    created = asyncio.Event()
    allow_creation = asyncio.Event()

    async def delayed_creation(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        created.set()
        await allow_creation.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_creation)
    task = asyncio.create_task(run_process(str(script), str(output), input_text="transaction\n", interruptible=False))
    try:
        await asyncio.wait_for(created.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        allow_creation.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert output.read_text() == "transaction\n"


if __name__ == "__main__":
    pytest_bazel.main()
