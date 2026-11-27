"""Bounded host commands that never outlive their caller."""

import asyncio
import shlex
from collections.abc import Sequence


async def run_host_command(argv: Sequence[str], *, timeout_s: float) -> bytes:
    """Return stdout of a successful `argv`; kill and reap it if the caller times out or is cancelled."""
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"{shlex.join(argv)} exited {process.returncode}: {stderr.decode(errors='replace')[-2000:]}")
    return stdout
