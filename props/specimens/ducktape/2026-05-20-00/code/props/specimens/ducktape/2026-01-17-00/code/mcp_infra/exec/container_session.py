from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiodocker
import anyio
from fastmcp.server import FastMCP

from mcp_infra.constants import SLEEP_FOREVER_CMD, WORKING_DIR
from mcp_infra.exec.models import MAX_BYTES_CAP, BaseExecResult, ExecInput, render_raw_to_result

logger = logging.getLogger(__name__)

CONTAINER_STATUS_POLL_INTERVAL_SECS = 0.05
KILL_RETRY_DELAY_SECS = 0.2
CONTAINER_RESTART_DELAY_SECS = 0.5

# Docker exec stream type codes
STREAM_TYPE_STDOUT = 1
STREAM_TYPE_STDERR = 2


@dataclass(frozen=True)
class BindMount:
    """Type-safe Docker volume bind mount specification.

    Represents a single volume mount from host to container.
    Internal representation uses Path objects for type safety.
    """

    host_path: Path
    container_path: Path
    mode: str = "rw"

    def to_docker_spec(self) -> str:
        """Convert to Docker bind spec string: 'host:container:mode'."""
        return f"{self.host_path}:{self.container_path}:{self.mode}"

    @classmethod
    def parse_binds(cls, values: list[str] | None) -> list[BindMount] | None:
        """Parse bind mount specifications into BindMount objects.

        Args:
            values: List of bind specs in format "host:container[:mode]"

        Returns:
            List of BindMount objects, or None if no binds

        Raises:
            ValueError: If bind spec format is invalid
        """
        if not values:
            return None
        result: list[BindMount] = []
        entries: list[str] = []
        for value in values:
            entries.extend(value.split(","))
        for entry in entries:
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) < 2:
                raise ValueError(f"Invalid bind mount spec '{entry}'. Use host:container[:mode].")
            host, container, *mode_parts = parts
            result.append(
                cls(
                    host_path=Path(host).resolve(),
                    container_path=Path(container),
                    mode=mode_parts[0] if mode_parts else "rw",
                )
            )
        return result


@dataclass
class ContainerSessionState:
    docker_client: aiodocker.Docker
    container_id: str | None
    image: str
    # Volume bind mounts for the container
    binds: list[BindMount] | None
    # Container working directory
    working_dir: Path
    # Network mode used to start the container (str: "none", "bridge", "host", or custom network name)
    network_mode: str
    # Environment variables for the container
    environment: dict[str, str] | None


@dataclass
class ContainerOptions:
    image: str
    working_dir: Path = WORKING_DIR
    binds: list[BindMount] | None = None
    network_mode: str = "none"
    environment: dict[str, str] | None = None
    labels: dict[str, str] | None = None
    name: str | None = None
    auto_remove: bool = False
    # TODO: replace this implicit session-scoped default with an explicit
    # lifecycle enum (e.g., externally_provided, server_scoped, session_scoped,
    # call_scoped) once we need other strategies.

    def to_container_config(
        self,
        *,
        cmd: list[str],
        working_dir: Path | None = None,
        env: dict[str, str] | None = None,
        auto_remove: bool | None = None,
    ) -> dict[str, Any]:
        """Build Docker container config dict.

        Args:
            cmd: Command to run (list of strings)
            working_dir: Override container working directory (uses self.working_dir if None)
            env: Override environment variables (uses self.environment if None)
            auto_remove: Whether to auto-remove container after exit (defaults to self.auto_remove)

        Returns:
            Docker container config dict ready for containers.create()
        """
        return {
            "Image": self.image,
            "Cmd": cmd,
            "WorkingDir": str(working_dir if working_dir is not None else self.working_dir),
            "Env": [f"{k}={v}" for k, v in (env or self.environment or {}).items()],
            "Labels": self.labels or {},
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": False,
            "HostConfig": _build_host_config(
                self, auto_remove=self.auto_remove if auto_remove is None else auto_remove
            ),
        }


def session_state_from_ctx(ctx: Any) -> ContainerSessionState:
    return cast(ContainerSessionState, ctx.request_context.lifespan_context)


def _build_host_config(opts: ContainerOptions, *, auto_remove: bool = False) -> dict[str, Any]:
    """Build Docker HostConfig from ContainerOptions.

    Args:
        opts: Container options with binds and network_mode
        auto_remove: Whether to set AutoRemove (for per-session containers)

    Returns:
        Docker HostConfig dict with Binds and NetworkMode if applicable
    """
    host_config: dict[str, Any] = {}

    if auto_remove:
        host_config["AutoRemove"] = True

    # Convert binds to Docker HostConfig format
    if opts.binds:
        host_config["Binds"] = [bind.to_docker_spec() for bind in opts.binds]

    host_config["NetworkMode"] = opts.network_mode

    return host_config


async def _create_and_start_container(client: aiodocker.Docker, opts: ContainerOptions) -> str:
    """Create and start a Docker container with cleanup on start failure.

    Args:
        client: aiodocker Docker client
        opts: Container configuration options

    Returns:
        Container ID (string)

    Raises:
        Exception: If container creation or start fails (container is cleaned up first)

    Note:
        If start() fails after create() succeeds, the container is immediately
        cleaned up before re-raising the exception. This prevents container leaks.
    """
    # Always set auto_remove=False - we handle cleanup explicitly to ensure
    # containers are removed even if the process crashes before normal exit.
    container_config = opts.to_container_config(cmd=SLEEP_FOREVER_CMD, auto_remove=False)

    container = await client.containers.create(container_config, name=opts.name)
    container_id = container.id

    try:
        await container.start()
        return container_id
    except Exception:
        # Container created but start failed - clean it up before re-raising
        try:
            await container.delete(force=True)
            logger.debug(f"Cleaned up failed container {container_id}")
        except Exception as cleanup_error:
            logger.error(f"Failed to cleanup container {container_id}: {cleanup_error}")
        raise


@asynccontextmanager
async def scoped_container(client: aiodocker.Docker, opts: ContainerOptions):
    """Create, start, and manage a Docker container's lifecycle.

    Guarantees cleanup even if start fails. Yields container ID.

    Args:
        client: aiodocker Docker client
        opts: Container configuration options

    Yields:
        Container ID (string)

    Note:
        Always cleans up the container in __aexit__, even if start() fails.
        This prevents container leaks when initialization errors occur.
    """
    container_id = await _create_and_start_container(client, opts)

    try:
        yield container_id
    finally:
        # Always clean up when exiting scope
        with anyio.CancelScope(shield=True):
            try:
                container = await client.containers.get(container_id)
                # Check if container is running before trying to kill
                info = await container.show()
                status = info["State"]["Status"]
                if status == "running":
                    await container.kill()
                    logger.debug(f"Container {container_id} killed")
                else:
                    logger.debug(f"Container {container_id} already stopped (status: {status})")

                await container.delete(force=True)
                logger.debug(f"Container {container_id} cleaned up successfully")
            except Exception as e:
                logger.error(f"Container cleanup failed for {container_id}: {e}")


# ---- Lifespan factory (per-session container) ----


def make_container_lifespan(opts: ContainerOptions, docker_client: aiodocker.Docker):
    """Create lifespan context manager for container session.

    Args:
        opts: Container configuration options
        docker_client: Async Docker client (owned by caller, not closed by lifespan)

    Returns:
        Lifespan context manager that yields ContainerSessionState

    Note:
        The caller owns docker_client and manages its lifecycle. The lifespan only
        manages the container created from opts. Container cleanup is delegated to
        scoped_container() which guarantees cleanup even on initialization failures.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP):  # yields ContainerSessionState
        async with scoped_container(docker_client, opts) as container_id:
            yield ContainerSessionState(
                docker_client=docker_client,
                container_id=container_id,
                image=opts.image,
                binds=opts.binds,
                working_dir=opts.working_dir,
                network_mode=opts.network_mode,
                environment=opts.environment,
            )

    return lifespan


# Module-level ExecInput to avoid ForwardRef issues during FastMCP signature introspection


# ---- Helpers (small, focused) ----------------------------------------------


async def _race_with_timeout(work_task: asyncio.Task, timeout_ms: float) -> bool:
    """Race a work task against a timeout.

    Args:
        work_task: The async work to race
        timeout_ms: Timeout in milliseconds

    Returns:
        True if timeout occurred, False if work completed first
    """
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_ms / 1000.0))

    done, pending = await asyncio.wait({timeout_task, work_task}, return_when=asyncio.FIRST_COMPLETED)

    # Cancel pending tasks
    for task in pending:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    return timeout_task in done


async def _kill_container_with_retry(container) -> None:
    """Kill container with retry for stubborn processes.

    Attempts to kill twice with a delay between attempts.
    Suppresses all exceptions since this is best-effort cleanup.

    Args:
        container: aiodocker container instance
    """
    try:
        await container.kill()
        await asyncio.sleep(KILL_RETRY_DELAY_SECS)
        await container.kill()
    except (aiodocker.exceptions.DockerError, asyncio.CancelledError):
        # Container already stopped or kill cancelled - ignore during cleanup
        pass


def _normalize_docker_logs_to_bytes(logs) -> bytes:
    """Normalize Docker log output to bytes.

    Docker logs API can return various formats. This normalizes to bytes.

    Args:
        logs: Log data from Docker (list, bytes, str, or None)

    Returns:
        Normalized bytes
    """
    if logs is None:
        return b""

    if isinstance(logs, bytes):
        return logs

    if isinstance(logs, str):
        return logs.encode("utf-8")

    if isinstance(logs, list):
        # Concatenate all chunks
        result = bytearray()
        for chunk in logs:
            if isinstance(chunk, bytes):
                result.extend(chunk)
            elif isinstance(chunk, str):
                result.extend(chunk.encode("utf-8"))
            else:
                logger.warning(f"Unexpected chunk type in logs list: {type(chunk)}")
        return bytes(result)

    logger.warning(f"Unexpected logs type: {type(logs)}, returning empty bytes")
    return b""


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
    """Render container execution output to BaseExecResult."""
    return render_raw_to_result(
        stdout=stdout_buf,
        stderr=stderr_buf,
        exit_code=exit_code,
        timed_out=timed_out,
        max_bytes=MAX_BYTES_CAP,
        duration_ms=duration_ms,
    )


async def run_session_container(
    s: ContainerSessionState, cmd: list[str], input: ExecInput, opts: ContainerOptions
) -> tuple[bytearray, bytearray, int | None, bool]:
    """Run command in per-session container using aiodocker exec."""
    container_id = s.container_id
    if container_id is None:
        raise RuntimeError("No per-session container available")

    logger.debug(f"Executing command in container {container_id[:12]}: {cmd!r} (timeout_ms={input.timeout_ms})")

    docker_client = s.docker_client
    container_instance = await docker_client.containers.get(container_id)

    # Execute with timeout handling
    loop = asyncio.get_running_loop()
    overall_start = loop.time()
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    timed_out = False
    exit_code = None

    # Create exec instance with explicit args (avoid **kwargs issues)
    t0 = loop.time()
    exec_obj = await container_instance.exec(
        cmd,
        stdout=True,
        stderr=True,
        stdin=False,
        tty=False,  # No TTY to ensure stdout/stderr separation
        workdir=str(input.cwd) if input.cwd is not None else str(s.working_dir),
        environment=input.env_dict(),
        user=input.user or "",
    )
    t_exec_created = loop.time()

    # Start exec and collect output with timeout
    stream_start = loop.time()
    stream: Any = exec_obj.start()

    # Implement external timeout mechanism
    collect_task = asyncio.create_task(_collect_from_exec_stream(stream, stdout_buf, stderr_buf))
    t_collect_begin = loop.time()
    timed_out = await _race_with_timeout(collect_task, input.timeout_ms)
    t_collect_end = loop.time()

    if timed_out:
        # External timeout - kill the exec process, not the container
        logger.debug(f"Command timed out after {input.timeout_ms}ms in container {container_id[:12]}")
        exit_code = None

        # Get the PID of the exec process and kill it
        inspect_result = await exec_obj.inspect()
        pid = inspect_result.get("Pid")
        if pid and pid > 0:
            # Kill the specific process inside the container
            kill_exec = await container_instance.exec(["kill", "-9", str(pid)], stdout=False, stderr=False, stdin=False)
            kill_stream = kill_exec.start()
            # Drain the stream to ensure the kill command completes
            while await kill_stream.read_out() is not None:
                pass
            logger.debug(f"Killed exec process PID {pid} in container {container_id[:12]}")
        else:
            raise RuntimeError(f"Could not get PID for timed-out exec in container {container_id[:12]}")
    else:
        # Command completed normally - inspect exec for exit code
        t_inspect_begin = loop.time()
        inspect_result = await exec_obj.inspect()
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
        (stream_start - t_exec_created) * 1000,
        (t_collect_end - t_collect_begin) * 1000,
        len(stdout_buf),
        len(stderr_buf),
    )

    return stdout_buf, stderr_buf, exit_code, timed_out
