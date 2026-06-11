"""Streaming command execution with heartbeat output."""

from __future__ import annotations

import logging
import os
import select
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 5


def run_streaming(
    cmd: list[str | Path], operation: str | None = None, check: bool = True, env: dict[str, str] | None = None
) -> int:
    """Run command with real-time streaming output.

    Args:
        cmd: Command and arguments (accepts str or Path)
        operation: Description for logs (defaults to first element of cmd)
        check: Raise RuntimeError on non-zero exit
        env: Additional environment variables
    """
    cmd_strs = [str(c) for c in cmd]
    op = operation or cmd_strs[0]

    logger.info("run: %s", " ".join(cmd_strs))

    merged_env = {**os.environ, **(env or {})}

    proc = subprocess.Popen(
        cmd_strs, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=merged_env
    )

    if proc.stdout is None:
        raise RuntimeError("Failed to create pipe for subprocess output")

    while True:
        ready, _, _ = select.select([proc.stdout], [], [], HEARTBEAT_INTERVAL_SECONDS)

        if ready:
            line = proc.stdout.readline().rstrip("\n\r")
            if not line:
                break
            if line:
                logger.info("%s", line)
        else:
            logger.info("%s: still running", op)

    proc.wait()

    if proc.returncode == 0:
        logger.info("%s: done", op)
    else:
        logger.error("%s: failed with code %d", op, proc.returncode)
        if check:
            raise RuntimeError(f"{op} failed with exit code {proc.returncode}")

    return proc.returncode
