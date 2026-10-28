from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import aiodocker
import anyio
from fastmcp.server import FastMCP
from tenacity import AsyncRetrying, retry_if_result, wait_fixed

from mcp_infra.constants import SLEEP_FOREVER_CMD
from mcp_infra.exec.docker.types import ContainerExecServerConfig, ResolvedExecInput
from util.exec.models import MAX_BYTES_CAP, BaseExecResult, render_raw_to_result

logger = logging.getLogger(__name__)

# Docker exec stream type codes
STREAM_TYPE_STDOUT = 1
STREAM_TYPE_STDERR = 2
DOCKER_EXIT_GRACE_S = 5


@dataclass
class ContainerSessionState:
    docker_client: aiodocker.Docker
    container_id: str | None
    opts: ContainerExecServerConfig


def session_state_from_ctx(ctx: Any) -> ContainerSessionState:
    return cast(ContainerSessionState, ctx.request_context.lifespan_context)


async def _create_and_start_container(client: aiodocker.Docker, config: ContainerExecServerConfig) -> str:
    """Create and start a Docker container with cleanup on start failure.

    If start() fails after create() succeeds, the container is immediately
    cleaned up before re-raising the exception. This prevents container leaks.
    """
    # Always set auto_remove=False - we handle cleanup explicitly to ensure
    # containers are removed even if the process crashes before normal exit.
    container_config = config.to_container_config(cmd=SLEEP_FOREVER_CMD, auto_remove=False)

    container = await client.containers.create(container_config, name=config.name)
    container_id = container.id

    try:
        await container.start()
        return container_id
    except Exception:
        try:
            await container.delete(force=True)
            logger.debug(f"Cleaned up failed container {container_id}")
        except Exception:
            logger.exception(f"Failed to cleanup container {container_id}")
        raise


@asynccontextmanager
async def scoped_container(client: aiodocker.Docker, config: ContainerExecServerConfig):
    """Create, start, and manage a Docker container's lifecycle.

    Guarantees cleanup even if start fails. Yields container ID.
    """
    container_id = await _create_and_start_container(client, config)

    try:
        yield container_id
    finally:
        with anyio.CancelScope(shield=True):
            try:
                container = await client.containers.get(container_id)
                info = await container.show()
                status = info["State"]["Status"]
                if status == "running":
                    await container.kill()
                    logger.debug(f"Container {container_id} killed")
                else:
                    logger.debug(f"Container {container_id} already stopped (status: {status})")

                await container.delete(force=True)
                logger.debug(f"Container {container_id} cleaned up successfully")
            except Exception:
                logger.exception(f"Container cleanup failed for {container_id}")


# ---- Lifespan factory (per-session container) ----


def make_container_lifespan(config: ContainerExecServerConfig, docker_client: aiodocker.Docker):
    """Create lifespan context manager for container session.

    The caller owns docker_client and manages its lifecycle. The lifespan only
    manages the container created from config.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP):  # yields ContainerSessionState
        async with scoped_container(docker_client, config) as container_id:
            yield ContainerSessionState(docker_client=docker_client, container_id=container_id, opts=config)

    return lifespan


# ---- Helpers (small, focused) ----------------------------------------------


async def _collect_from_exec_stream(stream, stdout_buf: bytearray, stderr_buf: bytearray) -> None:
    """Read from multiplexed Docker exec stream into separate buffers."""
    while True:
        chunk = await stream.read_out()
        if chunk is None:
            break

        data = chunk.data
        if not isinstance(data, bytes):
            logger.warning(f"Expected bytes from exec stream, got {type(data)}, converting")
            data = data.encode("utf-8") if isinstance(data, str) else bytes(data)

        if chunk.stream == STREAM_TYPE_STDOUT:
            stdout_buf.extend(data)
        elif chunk.stream == STREAM_TYPE_STDERR:
            stderr_buf.extend(data)
        else:
            logger.warning(f"Unknown stream type {chunk.stream}, defaulting to stdout")
            stdout_buf.extend(data)


def render_container_result(
    stdout_buf: bytearray, stderr_buf: bytearray, exit_code: int | None, timed_out: bool, duration_ms: int
) -> BaseExecResult:
    return render_raw_to_result(
        stdout=bytes(stdout_buf),
        stderr=bytes(stderr_buf),
        exit_code=exit_code,
        timed_out=timed_out,
        max_bytes=MAX_BYTES_CAP,
        duration_ms=duration_ms,
    )


async def run_session_container(
    session: ContainerSessionState, cmd: list[str], input: ResolvedExecInput
) -> tuple[bytearray, bytearray, int | None, bool]:
    """Run command in per-session container using aiodocker exec."""
    container_id = session.container_id
    if container_id is None:
        raise RuntimeError("No per-session container available")

    logger.debug(f"Executing command in container {container_id[:12]}: {cmd!r} (timeout_ms={input.timeout_ms})")

    container_instance = await session.docker_client.containers.get(container_id)

    loop = asyncio.get_running_loop()
    overall_start = loop.time()
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    timeout_s = input.timeout_ms / 1000

    # Create exec instance with explicit args (avoid **kwargs issues)
    t0 = loop.time()
    exec_obj = await container_instance.exec(
        ["/usr/bin/timeout", "--signal=KILL", f"{timeout_s}s", *cmd],
        stdout=True,
        stderr=True,
        stdin=False,
        tty=False,  # No TTY to ensure stdout/stderr separation
        workdir=str(input.cwd) if input.cwd is not None else str(session.opts.working_dir),
        environment=input.env_dict(),
        user=input.user or "",
    )
    t_exec_created = loop.time()

    stream_start = loop.time()
    stream: Any = exec_obj.start()

    t_collect_begin = loop.time()
    try:
        # The in-container utility enforces the process deadline. This outer
        # deadline only bounds Docker transport/exit reporting; expiry raises.
        async with asyncio.timeout(timeout_s + DOCKER_EXIT_GRACE_S):
            await _collect_from_exec_stream(stream, stdout_buf, stderr_buf)
    finally:
        # start() is lazy: cancellation during read_out() can interrupt stream
        # initialization, before Stream.__aenter__ could register __aexit__.
        await stream.close()
    t_collect_end = loop.time()

    inspect_result: dict[str, Any] = await asyncio.wait_for(
        AsyncRetrying(wait=wait_fixed(0.05), retry=retry_if_result(lambda info: info["Running"]))(exec_obj.inspect),
        DOCKER_EXIT_GRACE_S,
    )
    exit_code = inspect_result["ExitCode"]
    if exit_code is None:
        raise RuntimeError(f"Docker exec completed without an exit code in container {container_id[:12]}")
    # SIGKILL also kills GNU timeout itself (137). Preserve an early command
    # exit with that status; an exit at/after the deadline is ambiguous.
    timed_out = exit_code == 137 and t_collect_end - t_collect_begin >= timeout_s
    if timed_out:
        exit_code = None

    overall_elapsed = (loop.time() - overall_start) * 1000
    logger.info(
        (
            "container exec finished: cmd=%r exit_code=%s timed_out=%s "
            "elapsed_ms=%.1f create_ms=%.1f start_ms=%.1f collect_ms=%.1f stdout_bytes=%d stderr_bytes=%d"
        ),
        cmd,
        exit_code,
        timed_out,
        overall_elapsed,
        (t_exec_created - t0) * 1000,
        (stream_start - t_exec_created) * 1000,
        (t_collect_end - t_collect_begin) * 1000,
        len(stdout_buf),
        len(stderr_buf),
    )

    return stdout_buf, stderr_buf, exit_code, timed_out
