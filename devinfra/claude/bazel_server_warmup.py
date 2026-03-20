"""Background Bazel server warmup for Claude Code sessions.

Starts the Bazel server by running `bazel info` so the first real command
doesn't pay the JVM startup cost.
"""

import asyncio
import logging
import subprocess
from pathlib import Path

from util.bazel.workspace import BazelInfoResult, BazelWorkspace

logger = logging.getLogger(__name__)

_WARMUP_TIMEOUT_SECS = 120


async def warmup_bazel_server(wrapper_path: Path, project_dir: Path, env_file: Path) -> BazelInfoResult:
    """Fire-and-forget Bazel server warmup. Logs errors, never raises.

    Uses the bazel wrapper so --bazelrc, proxy credentials, and session env
    vars are applied (sourced from *env_file*). Runs as an async subprocess
    with a timeout.
    """
    logger.info("Warming up Bazel server (wrapper=%s, project=%s)", wrapper_path, project_dir)
    workspace = BazelWorkspace(root=project_dir, binary=str(wrapper_path), env_file=env_file)
    try:
        async with asyncio.timeout(_WARMUP_TIMEOUT_SECS):
            result = await workspace.info()
    except TimeoutError:
        logger.warning("Bazel server warmup timed out")
        return BazelInfoResult()
    except subprocess.CalledProcessError as e:
        logger.warning("Bazel server warmup failed (exit=%d): %s", e.returncode, (e.stderr or "").strip())
        return BazelInfoResult()
    except Exception as e:
        logger.warning("Bazel server warmup failed: %s", e)
        return BazelInfoResult()

    logger.info("Bazel server warm (pid=%s, output_base=%s)", result.server_pid, result.output_base)
    return result
