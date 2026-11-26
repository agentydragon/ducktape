"""Host commands end with their caller and report failure."""

import asyncio
import os
import sys

import pytest
import pytest_bazel

from agentplane.system_tests.driver.host_command import run_host_command

# Announces its PID through the FIFO named by argv[1], then outlives any test.
HUNG_COMMAND = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(3600)"


async def test_cancellation_kills_and_reaps_the_command(tmp_path) -> None:
    ready = tmp_path / "ready"
    os.mkfifo(ready)
    task = asyncio.create_task(run_host_command((sys.executable, "-c", HUNG_COMMAND, str(ready)), timeout_s=60))
    pid = int(await asyncio.to_thread(ready.read_text))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_failed_command_raises() -> None:
    with pytest.raises(RuntimeError, match="exited 3"):
        await run_host_command((sys.executable, "-c", "raise SystemExit(3)"), timeout_s=60)


if __name__ == "__main__":
    pytest_bazel.main()
