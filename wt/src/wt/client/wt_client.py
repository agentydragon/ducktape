"""wt daemon client for fast working directory status.

This client connects to the wt multiplexing daemon via socket,
providing both low-level daemon communication and high-level status operations.
"""

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import IO, TypeVar, cast
import uuid

import click
import psutil
from pydantic import BaseModel, TypeAdapter, ValidationError
import pygit2

from ..shared.configuration import Configuration
from ..shared.env import is_test_mode
from ..shared.error_handling import validate_worktree_name
from ..shared.protocol import (
    ErrorCodes,
    ErrorResponse,
    HookOutputEvent,
    HookRunResult,
    ProgressEvent,
    Request,
    Response,
    StartupMessage,
    StatusParams,
    StatusResponse,
    TeleportCdThere,
    TeleportDoesNotExist,
    TeleportResult,
    WorktreeCreateParams,
    WorktreeCreateResult,
    WorktreeDeleteParams,
    WorktreeDeleteResult,
    WorktreeGetByNameParams,
    WorktreeGetByNameResult,
    WorktreeID,
    WorktreeIdentifyParams,
    WorktreeIdentifyResult,
    WorktreeListResult,
    WorktreeResolvePathParams,
    WorktreeResolvePathResult,
    WorktreeTeleportTargetParams,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def read_daemon_pid(pid_path: Path) -> int | None:
    """Read daemon PID from file.

    Returns:
        PID as int if file exists and contains valid PID, None otherwise.
    """
    if not pid_path.exists():
        return None

    try:
        pid_str = await asyncio.to_thread(pid_path.read_text)
        pid_str = pid_str.strip()

        if not pid_str:
            return None

        return int(pid_str)
    except (OSError, ValueError):
        return None


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class WtClient:
    """JSON-RPC client for communicating with the worktree management daemon."""

    config: Configuration
    verbose: bool = False
    _handshake_pipe: int | None = field(default=None, init=False)
    _progress_callback: Callable[[ProgressEvent], None] | None = field(default=None, init=False)
    _hook_output_callback: Callable[[HookOutputEvent], None] | None = field(default=None, init=False)

    # Class-level lock to prevent multiple daemon startups
    _daemon_start_lock = asyncio.Lock()

    def __post_init__(self) -> None:
        # Ensure daemon directory exists
        self.config.wt_dir.mkdir(exist_ok=True)

    def set_progress_callback(self, cb: Callable[[ProgressEvent], None] | None) -> None:
        self._progress_callback = cb

    def set_hook_output_callback(self, cb: Callable[[HookOutputEvent], None] | None) -> None:
        self._hook_output_callback = cb

    async def current_worktree_info(self) -> tuple[Path | None, str | None]:
        res = await self.identify_worktree(Path.cwd())
        if not res.is_worktree or not res.name:
            return None, None
        by_name = await self.get_worktree_by_name(res.name)
        if by_name.exists and by_name.absolute_path:
            return by_name.absolute_path, (res.relative_path or None)
        return None, None

    async def _is_daemon_running(self) -> bool:
        """Check if the daemon is running."""
        try:
            pid = await read_daemon_pid(self.config.daemon_pid_path)
            if pid is None:
                return False

            # Check if process exists and socket is accessible
            return bool(psutil.pid_exists(pid) and self.config.daemon_socket_path.exists())

        except (ValueError, OSError):
            return False

    async def _start_daemon_if_needed(self) -> None:
        """Start daemon if not running."""

        async with self._daemon_start_lock:
            if await self._is_daemon_running():
                logger.debug("Daemon already running for %s", self.config.main_repo)
                return

            logger.info("Starting wt daemon for %s", self.config.main_repo)
            logger.debug("Daemon socket: %s", self.config.daemon_socket_path)
            logger.debug("Daemon logs: %s", self.config.daemon_log_file)
            logger.info("wt: starting daemon … (%s)", self.config.daemon_socket_path)

            # Use handshake pipe to get immediate readiness without busy-wait
            await self._start_daemon_background()

            try:
                handshake_msg = await asyncio.wait_for(
                    self._read_handshake_from_pipe(), timeout=self.config.startup_timeout.total_seconds()
                )

                if handshake_msg.protocol_version != 1:
                    raise RuntimeError(
                        f"Incompatible daemon protocol version {handshake_msg.protocol_version}, expected 1"
                    )

                if handshake_msg.success and handshake_msg.ready:
                    logger.info("Daemon startup handshake ok (ready)")
                    return
                if not handshake_msg.success:
                    error_message = handshake_msg.error or "Unknown startup error"
                    raise RuntimeError(f"Daemon startup failed:\n{error_message}")
                # Guard: success without ready indicates premature closure; treat as error to avoid races
                raise RuntimeError("Daemon startup did not signal ready")

            except TimeoutError as e:
                timeout_secs = self.config.startup_timeout.total_seconds()
                diag = self._collect_daemon_diagnostics()
                msg = [f"Daemon startup timed out after {timeout_secs:.1f} seconds"]
                if diag:
                    msg.append(diag)
                raise RuntimeError("\n".join(msg)) from e
            except (OSError, RuntimeError, ValueError) as e:
                diag = self._collect_daemon_diagnostics()
                raise RuntimeError("Daemon startup failed.\n" + diag if diag else "Daemon startup failed.") from e
            finally:
                self._handshake_pipe = None

    async def _start_daemon_background(self) -> None:
        """Start daemon in background with a dedicated handshake pipe (no double-fork).

        Implementation: create a pipe, launch wt.server.wt_server via subprocess.Popen,
        pass the write-end FD using pass_fds and WT_HANDSHAKE_FD so the daemon can emit
        JSON StartupMessage lines. Keep the read-end in this process for synchronous readiness.
        """
        # Create pipe for handshake communication (dedicated FD)
        read_fd, write_fd = os.pipe()

        # Set read end to non-blocking for asyncio compatibility
        flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        with contextlib.suppress(Exception):
            os.set_inheritable(write_fd, True)

        env = os.environ.copy()
        env["WT_HANDSHAKE_FD"] = str(write_fd)

        # Launch daemon as a new session; do not inherit stdio; only the handshake FD is kept
        log_stack = contextlib.ExitStack()
        # Explicitly annotate to satisfy mypy (Popen accepts int or IO[bytes])
        stdout_dest: int | IO[bytes]
        stderr_dest: int | IO[bytes]
        try:
            # In test mode, capture daemon stdout/stderr to WT_DIR/daemon.log for diagnostics
            if is_test_mode():
                log_path = Path(self.config.daemon_log_file)
                log_file = await asyncio.to_thread(log_path.open, "ab", 0)
                log_stack.enter_context(log_file)
                stdout_dest = log_file
                stderr_dest = log_file
            else:
                stdout_dest = subprocess.DEVNULL
                stderr_dest = subprocess.DEVNULL

            subprocess.Popen(  # noqa: ASYNC220
                [sys.executable, "-m", "wt.server.wt_server"],
                env=env,
                stdout=stdout_dest,
                stderr=stderr_dest,
                start_new_session=True,
                pass_fds=(write_fd,),
                close_fds=True,
            )
        finally:
            # Parent keeps only the read end; close write end in parent
            with contextlib.suppress(Exception):
                os.close(write_fd)
            log_stack.close()

        # Store read pipe so _read_handshake_from_pipe can consume it
        self._handshake_pipe = read_fd

    async def _read_handshake_from_pipe(self) -> StartupMessage:
        """Read streaming JSON messages from pipe until ready or failure.

        Daemon emits JSON lines: progress updates until {success:True, ready:True} or {success:False}.
        Returns the terminal message.
        """
        if not self._handshake_pipe:
            raise RuntimeError("No handshake pipe available")

        # Create async stream reader from file descriptor
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)

        # Open file descriptor in binary mode for async reading (FD already set to non-blocking)
        pipe_file = os.fdopen(self._handshake_pipe, "rb", buffering=0)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe_file)

        msg: StartupMessage | None = None

        try:
            while True:
                line_bytes = await reader.readline()
                if not line_bytes:
                    # EOF - pipe closed
                    break

                line = line_bytes.decode("utf-8")

                if self.verbose:
                    click.echo(f"[daemon-handshake] {line.rstrip()}")

                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    msg = StartupMessage.model_validate_json(stripped)
                except ValidationError:
                    continue

                # Return immediately on terminal condition
                if not msg.success or msg.ready:
                    return msg
        finally:
            transport.close()

        # Pipe closed without terminal message
        diag = self._collect_daemon_diagnostics()
        error_msg = "Daemon closed handshake pipe before signaling ready"
        if diag:
            error_msg += "\n" + diag
        raise RuntimeError(error_msg)

    async def get_status(self, worktree_ids: list[WorktreeID] | None = None) -> StatusResponse:
        await self._start_daemon_if_needed()
        ids: list[WorktreeID] = cast(list[WorktreeID], worktree_ids) if worktree_ids is not None else []
        params = StatusParams(worktree_ids=ids)
        return await self._rpc("get_status", params, TypeAdapter(StatusResponse))

    async def get_working_directory_status(self, worktree_path: Path) -> tuple[list[str], list[str]]:
        """Get working directory status for a single worktree via daemon response flags."""
        # Use server-side identification to safely convert path to WorktreeID
        try:
            identify_result = await self.identify_worktree(worktree_path)
            if identify_result.wtid is None:
                return [], []
            status_response = await self.get_status([identify_result.wtid])
        except RpcError as e:
            # Only special-case unmanaged worktrees by code; otherwise bubble up
            if e.code == ErrorCodes.WORKTREE_NOT_FOUND:
                return [], []
            raise

        if not status_response.items:
            return [], []

        # Extract the single result
        item = next(iter(status_response.items.values()))
        result = item.status

        repo_path = result.absolute_path
        try:
            repository = pygit2.Repository(str(repo_path))
        except (pygit2.GitError, ValueError, TypeError):
            return [], []

        dirty_flags = {
            pygit2.GIT_STATUS_INDEX_MODIFIED,
            pygit2.GIT_STATUS_INDEX_DELETED,
            pygit2.GIT_STATUS_INDEX_RENAMED,
            pygit2.GIT_STATUS_INDEX_TYPECHANGE,
            pygit2.GIT_STATUS_INDEX_NEW,
            pygit2.GIT_STATUS_WT_MODIFIED,
            pygit2.GIT_STATUS_WT_DELETED,
            pygit2.GIT_STATUS_WT_RENAMED,
            pygit2.GIT_STATUS_WT_TYPECHANGE,
            pygit2.GIT_STATUS_CONFLICTED,
        }
        dirty_files: list[str] = []
        untracked_files: list[str] = []
        repo_root = Path(repo_path)
        for file_path, flags in repository.status().items():
            abs_path = str(repo_root / file_path)
            if flags & pygit2.GIT_STATUS_WT_NEW:
                untracked_files.append(abs_path)
                continue
            if any(flags & flag for flag in dirty_flags):
                dirty_files.append(abs_path)

        return dirty_files, untracked_files

    async def _read_jsonrpc_with_events(self, reader: asyncio.StreamReader) -> tuple[dict | None, list[str], list[str]]:
        """Read a mixed event/response stream; return (response_json, stdout, stderr)."""
        hook_stdout: list[str] = []
        hook_stderr: list[str] = []
        response_json: dict | None = None
        progress_cb = self._progress_callback
        hook_cb: Callable[[HookOutputEvent], None] | None = self._hook_output_callback
        while line := await reader.readline():
            text = line.decode().strip()
            if not text:
                continue
            obj = json.loads(text)
            ev = obj.get("event") if isinstance(obj, dict) else None
            if ev == "hook_output":
                hook_ev: HookOutputEvent = HookOutputEvent.model_validate(obj)
                if callable(hook_cb):
                    hook_cb(hook_ev)
                (hook_stdout if hook_ev.stream.value == "stdout" else hook_stderr).append(hook_ev.output)
                continue
            if ev == "progress":
                prog_ev: ProgressEvent = ProgressEvent.model_validate(obj)
                if callable(progress_cb):
                    progress_cb(prog_ev)
                continue
            response_json = obj
            break
        return response_json, hook_stdout, hook_stderr

    def _validate_post_hook(self, post: HookRunResult) -> None:
        """Validate and surface post-creation hook outcome; raise RuntimeError on failure."""

        def _echo_io() -> None:
            out = "\n".join(s for s in [post.stdout, post.stderr] if s)
            if out.strip():
                click.echo(out)

        streamed = bool(post.streamed)

        if (ec := post.exit_code) not in (None, 0):
            if not streamed:
                _echo_io()
            raise RuntimeError(f"Post-creation script failed with exit code {ec}")
        if err := post.error:
            if not streamed:
                _echo_io()
            if err == "timeout":
                if (ts := post.timeout_secs) is not None:
                    raise RuntimeError(f"Post-creation script timed out after {ts:.1f}s")
                raise RuntimeError("Post-creation script timed out")
            raise RuntimeError(f"Post-creation script error: {err}")
        if not post.ran:
            if not streamed:
                _echo_io()
            raise RuntimeError("Post-creation script did not run")

    async def create_worktree(
        self, name: str, source_wtid: WorktreeID | None = None, source_branch: str | None = None
    ) -> WorktreeCreateResult:
        """Create a new worktree via RPC."""
        await self._start_daemon_if_needed()
        if not self.config.daemon_socket_path.exists():
            for _ in range(20):  # up to ~200ms
                await asyncio.sleep(0.01)
                if self.config.daemon_socket_path.exists():
                    break
            else:
                diag = self._collect_daemon_diagnostics()
                msg = "Daemon socket not available"
                if diag:
                    msg += "\n" + diag
                raise RuntimeError(msg)

        request_id = uuid.uuid4()
        params = WorktreeCreateParams(name=name, source_wtid=source_wtid, source_branch=source_branch)
        request = Request(method="worktree_create", params=params.model_dump(), id=request_id)

        try:
            reader, writer = await asyncio.open_unix_connection(self.config.daemon_socket_path)
            try:
                # Send request
                writer.write(request.model_dump_json().encode())
                writer.write(b"\n")
                await writer.drain()

                (response_json, _hook_stdout, _hook_stderr) = await self._read_jsonrpc_with_events(reader)
                if not response_json:
                    raise RuntimeError("No response from daemon for worktree_create")
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

            try:
                if "error" in response_json:
                    error_response = ErrorResponse.model_validate(response_json)
                    raise RuntimeError(error_response.error.message)
                success_response = Response.model_validate(response_json)
                result = WorktreeCreateResult.model_validate(success_response.result)
                if post := result.post_hook:
                    self._validate_post_hook(post)
                return result
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as e:
                logger.exception("Failed to parse daemon worktree_create response")
                raise RuntimeError("Failed to parse daemon worktree_create response") from e

        except (TimeoutError, ConnectionError, FileNotFoundError, OSError) as e:
            if self.verbose:
                logger.exception("Failed to communicate with daemon for worktree_create")
            raise RuntimeError("Daemon worktree_create communication failed") from e

    async def delete_worktree(self, wtid: WorktreeID, *, force: bool = False) -> WorktreeDeleteResult:
        await self._start_daemon_if_needed()
        return await self._rpc(
            "worktree_delete", WorktreeDeleteParams(wtid=wtid, force=force), TypeAdapter(WorktreeDeleteResult)
        )

    async def list_worktrees(self) -> WorktreeListResult:
        await self._start_daemon_if_needed()
        return await self._rpc("worktree_list", {}, TypeAdapter(WorktreeListResult))

    async def identify_worktree(self, absolute_path: Path | str) -> WorktreeIdentifyResult:
        await self._start_daemon_if_needed()
        path = Path(absolute_path)
        return await self._rpc(
            "worktree_identify", WorktreeIdentifyParams(absolute_path=path), TypeAdapter(WorktreeIdentifyResult)
        )

    async def get_worktree_by_name(self, name: str) -> WorktreeGetByNameResult:
        return await self._rpc(
            "worktree_get_by_name", WorktreeGetByNameParams(name=name), TypeAdapter(WorktreeGetByNameResult)
        )

    async def _rpc(self, method: str, params_model: BaseModel | dict[str, object], result_adapter: TypeAdapter[T]) -> T:
        await self._start_daemon_if_needed()
        # Guard against a short race where the socket file is created just after the check.
        # Try a brief, bounded wait for the socket to appear to make CLI flows robust under load.
        if not self.config.daemon_socket_path.exists():
            for _ in range(20):  # up to ~200ms
                await asyncio.sleep(0.01)
                if self.config.daemon_socket_path.exists():
                    break
            else:
                raise RuntimeError("Daemon socket not available")
        if isinstance(params_model, BaseModel):
            params = params_model.model_dump()
        elif isinstance(params_model, dict):
            params = params_model
        else:
            params = {}
        req = Request(method=method, params=params, id=uuid.uuid4())
        try:
            reader, writer = await asyncio.open_unix_connection(self.config.daemon_socket_path)
            writer.write(req.model_dump_json().encode())
            writer.write(b"\n")
            await writer.drain()
            data = await reader.readline()
            text = data.decode().strip()
            writer.close()
            await writer.wait_closed()
            obj = json.loads(text)
            if "error" in obj:
                err = ErrorResponse.model_validate(obj)
                raise RpcError(err.error.code, err.error.message, err.id)
            resp = Response.model_validate(obj)
            return result_adapter.validate_python(resp.result)
        except (TimeoutError, ConnectionError, FileNotFoundError, OSError, json.JSONDecodeError, ValidationError) as e:
            logger.error("RPC %s failed: %s", method, e)
            diag = self._collect_daemon_diagnostics()
            base = f"RPC {method} failed ({e.code}): {e}" if isinstance(e, RpcError) else f"RPC {method} failed: {e}"
            if diag:
                base = base + "\n" + diag
            raise RuntimeError(base) from e

    async def resolve_path(self, params: WorktreeResolvePathParams) -> Path:
        result: WorktreeResolvePathResult = await self._rpc(
            "worktree_resolve_path", params, TypeAdapter(WorktreeResolvePathResult)
        )
        return result.absolute_path

    async def resolve_path_simple(self, worktree_name: str | None, path_spec: str) -> Path:
        params = WorktreeResolvePathParams(worktree_name=worktree_name, path_spec=path_spec, current_path=Path.cwd())
        return await self.resolve_path(params)

    def _collect_daemon_diagnostics(self) -> str:
        """Collect a short diagnostic summary including daemon.log tail.

        Returns a formatted string or empty string if nothing could be collected.
        """
        lines: list[str] = []
        try:
            daemon_log = self.config.daemon_log_file
            lines.append(f"daemon.log: {daemon_log}")
            if daemon_log.exists():
                tail = daemon_log.read_text(errors="ignore").splitlines()[-50:]
                lines.append("daemon.log (tail):\n" + "\n".join(tail))
        except OSError as e:
            lines.append(f"daemon.log read failed: {e}")
        try:
            lines.append(f"pid file exists: {self.config.daemon_pid_path.exists()}")
            if self.config.daemon_pid_path.exists():
                lines.append(f"pid file contents: {self.config.daemon_pid_path.read_text().strip()}")
            lines.append(f"socket exists: {self.config.daemon_socket_path.exists()}")
        except OSError as e:
            lines.append(f"pid/socket stat failed: {e}")
        return "\n".join(lines).strip()

    async def teleport_target(
        self, target_name: str, current_path: Path | str
    ) -> TeleportCdThere | TeleportDoesNotExist:
        return await self._rpc(
            "worktree_teleport_target",
            WorktreeTeleportTargetParams(target_name=target_name, current_path=Path(current_path)),
            TypeAdapter(TeleportResult),
        )

    async def require_worktree_exists(self, name: str) -> Path:
        res = await self.get_worktree_by_name(name)
        if not res.exists or not res.absolute_path:
            raise RuntimeError(f"Worktree '{name}' not found")
        return res.absolute_path

    async def create_worktree_convenience(
        self, name: str, *, source_name: str | None = None, from_default: bool = True, from_branch: str | None = None
    ) -> Path:
        validate_worktree_name(name)
        if source_name:
            src = await self.get_worktree_by_name(source_name)
            if not src.exists or not src.wtid:
                raise RuntimeError(f"Worktree '{source_name}' not found")
            result = await self.create_worktree(name, source_wtid=src.wtid, source_branch=from_branch)
            return result.absolute_path
        if from_default:
            result = await self.create_worktree(name, source_branch=from_branch)
            return result.absolute_path
        raise RuntimeError("Invalid create_worktree request: no source and from_default=False")

    async def remove_worktree_by_name(self, name: str, *, force: bool = False) -> None:
        listing = await self.list_worktrees()
        target = None
        for wt in listing.worktrees:
            if wt.name == name:
                target = wt.wtid
                break
        if target is None:
            raise RuntimeError(f"Worktree '{name}' not found")
        await self.delete_worktree(target, force=force)
