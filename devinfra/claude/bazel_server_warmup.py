"""Background Bazel server warmup for Claude Code sessions.

Starts the Bazel server by running `bazel info` so the first real command
doesn't pay the JVM startup cost.
"""

import asyncio
import logging
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)

_WARMUP_TIMEOUT_SECS = 120


async def warmup_bazel_server(wrapper_path: Path, project_dir: Path, env_file: Path) -> None:
    """Fire-and-forget Bazel server warmup. Logs errors, never raises.

    Sources *env_file* then runs the bazel wrapper so --bazelrc, proxy
    credentials, and session env vars are applied.
    """
    logger.info("Warming up Bazel server (wrapper=%s, project=%s)", wrapper_path, project_dir)
    bazel_cmd = shlex.join([str(wrapper_path), "info"])
    shell_cmd = f"source {shlex.quote(str(env_file))} && {bazel_cmd}"
    try:
        async with asyncio.timeout(_WARMUP_TIMEOUT_SECS):
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_dir),
            )
            _, stderr_bytes = await proc.communicate()
    except TimeoutError:
        logger.warning("Bazel server warmup timed out")
        return
    except Exception as e:
        logger.warning("Bazel server warmup failed: %s", e)
        return

    if proc.returncode != 0:
        logger.warning("Bazel server warmup failed (exit=%d): %s", proc.returncode, stderr_bytes.decode().strip())
        return

    logger.info("Bazel server warm")
