"""Run blocking filesystem and libgit2 work without abandoning it on cancellation."""

import asyncio
import locale
import os
import signal
import subprocess
from collections.abc import Callable
from contextlib import suppress


async def _wait_owned[T](task: asyncio.Task[T]) -> T:
    """Wait for owned work despite repeated cancellation of its caller."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def run_blocking[**P, T](func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    thread = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(thread)
    except asyncio.CancelledError:
        # Cancellation wins over errors, but no thread may outlive its owner.
        with suppress(Exception):
            await _wait_owned(thread)
        raise


async def run_process(
    *args: str, input_text: str | None = None, env: dict[str, str] | None = None, interruptible: bool = True
) -> subprocess.CompletedProcess[str]:
    """Own a subprocess group through creation, cancellation, and pipe drainage."""
    input_bytes = input_text.encode() if input_text is not None else None
    creation = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    )
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError as cancelled:
        # A cancelled creation may already have forked; obtain ownership before returning.
        try:
            process = await _wait_owned(creation)
        except Exception:
            raise cancelled
        if interruptible:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        communication = asyncio.create_task(process.communicate(input_bytes))
        with suppress(Exception):
            await _wait_owned(communication)
        raise

    communication = asyncio.create_task(process.communicate(input_bytes))
    try:
        stdout_bytes, stderr_bytes = await asyncio.shield(communication)
    except asyncio.CancelledError:
        if interruptible:
            # Descendants may inherit the pipes even after the direct child exits.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(Exception):
            await _wait_owned(communication)
        raise
    assert process.returncode is not None
    encoding = locale.getpreferredencoding(False)
    return subprocess.CompletedProcess(
        args, process.returncode, stdout_bytes.decode(encoding), stderr_bytes.decode(encoding)
    )
