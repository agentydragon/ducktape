from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

import aiodocker
import anyio
from aiodocker.containers import DockerContainer
from fastmcp.server import FastMCP
from tenacity import AsyncRetrying, retry_if_result, wait_fixed

from mcp_infra.constants import SLEEP_FOREVER_CMD
from mcp_infra.exec.docker.types import ContainerExecServerConfig, ResolvedExecInput
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.exec.models import MAX_BYTES_CAP, BaseExecResult, render_raw_to_result

logger = logging.getLogger(__name__)

# Docker exec stream type codes
STREAM_TYPE_STDOUT = 1
STREAM_TYPE_STDERR = 2
SUPERVISOR_RUNFILE = "mcp_infra/exec/docker/liveness_pipe/docker_exec_supervisor_/docker_exec_supervisor"
STARTUP_CLEANUP_TIMEOUT_S = 5
EXEC_CLEANUP_TIMEOUT_S = 5


def _supervisor_path(container_id: str) -> str:
    return f"/.ducktape_docker_exec_supervisor_{container_id}"


async def _stage_exec_supervisor(client: aiodocker.Docker, container: DockerContainer) -> None:
    image = await client.images.inspect((await container.show())["Image"])
    if image["Os"] != "linux" or image["Architecture"] != "amd64":
        raise RuntimeError(f"Docker exec supervisor requires linux/amd64, got {image['Os']}/{image['Architecture']}")
    executable = get_required_path(own_repo_rlocation(SUPERVISOR_RUNFILE)).read_bytes()
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        entry = tarfile.TarInfo(_supervisor_path(container.id).lstrip("/"))
        entry.size = len(executable)
        entry.mode = 0o755
        archive.addfile(entry, io.BytesIO(executable))
    await container.put_archive("/", archive_bytes.getvalue())


@dataclass
class ContainerSessionState:
    docker_client: aiodocker.Docker
    container_id: str | None
    opts: ContainerExecServerConfig
    pending_exec_tasks: set[asyncio.Task[None]] = field(default_factory=set)


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
        await _stage_exec_supervisor(client, container)
        return container_id
    except Exception:
        try:
            await container.delete(force=True)
            logger.debug(f"Cleaned up failed container {container_id}")
        except Exception:
            logger.exception(f"Failed to cleanup container {container_id}")
        raise


@asynccontextmanager
async def scoped_container(
    client: aiodocker.Docker,
    config: ContainerExecServerConfig,
    pending_exec_tasks: set[asyncio.Task[None]] | None = None,
):
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
            if pending_exec_tasks:
                _, pending = await asyncio.wait(pending_exec_tasks, timeout=STARTUP_CLEANUP_TIMEOUT_S)
                for task in pending:
                    task.cancel()
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=STARTUP_CLEANUP_TIMEOUT_S)
                if pending:
                    logger.error("%d Docker exec startup tasks did not settle after container deletion", len(pending))


# ---- Lifespan factory (per-session container) ----


def make_container_lifespan(config: ContainerExecServerConfig, docker_client: aiodocker.Docker):
    """Create lifespan context manager for container session.

    The caller owns docker_client and manages its lifecycle. The lifespan only
    manages the container created from config.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP):  # yields ContainerSessionState
        pending_exec_tasks: set[asyncio.Task[None]] = set()
        async with scoped_container(docker_client, config, pending_exec_tasks) as container_id:
            yield ContainerSessionState(
                docker_client=docker_client,
                container_id=container_id,
                opts=config,
                pending_exec_tasks=pending_exec_tasks,
            )

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


async def _collect_owned_stream(
    stream: Any, stdout_buf: bytearray, stderr_buf: bytearray, started: asyncio.Event, stop: asyncio.Event
) -> None:
    async with stream:
        started.set()
        if not stop.is_set():
            await _collect_from_exec_stream(stream, stdout_buf, stderr_buf)


async def _wait_for_exec_exit(exec_obj: aiodocker.execs.Exec) -> dict[str, Any]:
    return await asyncio.wait_for(
        AsyncRetrying(wait=wait_fixed(0.05), retry=retry_if_result(lambda info: info["Running"]))(exec_obj.inspect),
        EXEC_CLEANUP_TIMEOUT_S,
    )


async def _close_interrupted_exec(
    exec_obj: aiodocker.execs.Exec,
    stream: Any,
    collector: asyncio.Task[None],
    started: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    stop.set()
    start_wait = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait(
            {collector, start_wait}, timeout=STARTUP_CLEANUP_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise RuntimeError("Docker exec start did not settle; collector retains stream ownership")
        await stream.close()
        if collector.done():
            collector.exception()  # The original collector error is re-raised by the caller.
        else:
            await asyncio.wait_for(asyncio.shield(collector), EXEC_CLEANUP_TIMEOUT_S)
        info = await _wait_for_exec_exit(exec_obj)
        if info.get("ExitCode") == 125:
            raise RuntimeError("Docker exec supervisor reported cleanup failure (exit 125)")
    finally:
        start_wait.cancel()


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
    timed_out = False
    exit_code = None

    # The supervisor keeps stdin open as its liveness token. The requested
    # command receives /dev/null, preserving the public exec input contract.
    t0 = loop.time()
    exec_obj = await container_instance.exec(
        [_supervisor_path(container_id), *cmd],
        stdout=True,
        stderr=True,
        stdin=True,
        tty=False,  # No TTY to ensure stdout/stderr separation
        workdir=str(input.cwd) if input.cwd is not None else str(session.opts.working_dir),
        environment=input.env_dict(),
        user=input.user or "",
    )
    t_exec_created = loop.time()

    stream: Any = exec_obj.start()
    t_collect_begin = loop.time()
    started = asyncio.Event()
    stop = asyncio.Event()
    collector = asyncio.create_task(_collect_owned_stream(stream, stdout_buf, stderr_buf, started, stop))
    session.pending_exec_tasks.add(collector)

    def release_collector(task: asyncio.Task[None]) -> None:
        session.pending_exec_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # Observe a late startup failure after bounded cleanup reports it.

    collector.add_done_callback(release_collector)
    deadline = asyncio.timeout(input.timeout_ms / 1000.0)
    try:
        async with deadline:
            await asyncio.shield(collector)
    except BaseException as error:
        timed_out = isinstance(error, TimeoutError) and deadline.expired()
        with anyio.CancelScope(shield=True):
            try:
                await _close_interrupted_exec(exec_obj, stream, collector, started, stop)
            except Exception as cleanup_error:
                cleanup_error.add_note(f"Original exec interruption: {type(error).__name__}: {error}")
                raise RuntimeError(f"Docker exec cleanup failed for {container_id[:12]}") from cleanup_error
        if not timed_out:
            raise
    t_collect_end = loop.time()

    if timed_out:
        logger.debug(f"Command timed out after {input.timeout_ms}ms in container {container_id[:12]}")
        exit_code = None
    else:
        t_inspect_begin = loop.time()
        inspect_result = await _wait_for_exec_exit(exec_obj)
        exit_code = inspect_result.get("ExitCode", 0)
        t_inspect_end = loop.time()
        logger.debug(
            "Command completed: exit_code=%s stdout=%dB stderr=%dB inspect_ms=%.1f",
            exit_code,
            len(stdout_buf),
            len(stderr_buf),
            (t_inspect_end - t_inspect_begin) * 1000,
        )

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
        (t_collect_begin - t_exec_created) * 1000,
        (t_collect_end - t_collect_begin) * 1000,
        len(stdout_buf),
        len(stderr_buf),
    )

    return stdout_buf, stderr_buf, exit_code, timed_out
