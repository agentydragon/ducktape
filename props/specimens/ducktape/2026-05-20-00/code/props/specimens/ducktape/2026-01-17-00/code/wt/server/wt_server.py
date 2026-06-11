"""wt server: handles multiplexing gitstatusd, GitHub and worktree management.

One daemon per main git repository that:
- Auto-discovers worktrees by filesystem scanning
- Manages gitstatusd processes per worktree
- Provides socket-based API for CLI clients
- Handles concurrent requests efficiently
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..shared.configuration import Configuration, load_config
from ..shared.protocol import (
    ErrorCodes,
    ErrorResponse,
    GitstatusdAvailable,
    GitstatusdUnavailable,
    PingResult,
    Request,
    Response,
    WorktreeID,
    create_error_response,
    parse_request,
)
from .git_manager import GitManager
from .git_refs_watcher import GitRefsWatcher
from .github_client import GitHubInterface
from .github_watcher import GitHubWatcher
from .gitstatus_refresh import DebouncedGitstatusRefresh
from .gitstatusd_listener import GitstatusdListener

# Force import of handlers to register RPC methods
from .handlers import (
    path_handler,  # noqa: F401
    pr_handler,  # noqa: F401
    status_handler,  # noqa: F401
    worktree_handler,  # noqa: F401
)
from .repo_status import RepoStatus
from .rpc import rpc
from .services import DiscoveryService, GitstatusdService, WorktreeIndexService, scan_worktrees
from .stores import DaemonStore
from .types import DiscoveredWorktree
from .watcher import start_watcher
from .worktree_index import WorktreeIndex
from .worktree_registry import WorktreeRegistry
from .worktree_service import WorktreeService

logger = logging.getLogger(__name__)


def write_startup_handshake(
    success: bool,
    error_message: str | None = None,
    *,
    redirect_after: bool = True,
    daemon_log_path: Path | None = None,
    **extra_data,
):
    """Write startup handshake/progress JSON to dedicated pipe FD if provided."""

    fd_env = os.environ.get("WT_HANDSHAKE_FD")
    handshake_fd = None
    if fd_env and fd_env.isdigit():
        try:
            handshake_fd = int(fd_env)
        except (ValueError, TypeError):
            handshake_fd = None

    handshake_data = {"success": success, "pid": os.getpid(), "timestamp": time.time(), **extra_data}

    if not success and error_message:
        handshake_data["error"] = error_message

    payload = (json.dumps(handshake_data) + "\n").encode()
    if handshake_fd is None:
        return
    with contextlib.suppress(OSError):
        os.write(handshake_fd, payload)

    if redirect_after:
        try:
            daemon_log = daemon_log_path or (load_config().wt_dir / "daemon.log")
            log_fd = os.open(daemon_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(log_fd, 1)
            os.close(log_fd)
        except OSError:
            os.dup2(2, 1)
        logger.info("Startup handshake sent: success=%s", success)
        if not success:
            logger.error("Startup failed: %s", error_message)


class WtDaemon:
    """Main worktree management daemon that handles all worktree operations."""

    def __init__(self, config):
        self.config: Configuration = config
        logger.info(
            "Daemon configuration loaded - worktrees_dir: %s, github_repo: %s",
            self.config.worktrees_dir,
            self.config.github_repo,
        )
        logger.info(
            "GitHub refresh configuration - debounce_delay: %.1fs, periodic_interval: %.1fs",
            self.config.github_debounce_delay.total_seconds(),
            self.config.github_periodic_interval.total_seconds(),
        )
        logger.info("Post-creation script configuration: %s", self.config.post_creation_script or "None")

        # Initialize GitHub interface
        self.github_interface = None
        if self.config.github_enabled and self.config.github_repo:
            try:
                self.github_interface = GitHubInterface(self.config.github_repo)
                logger.info("GitHub interface initialized for repo: %s", self.config.github_repo)
            except (RuntimeError, OSError) as e:
                logger.warning("Failed to initialize GitHub interface for %s: %s", self.config.github_repo, e)
        # use self.config.wt_dir directly
        self.socket_path = self.config.daemon_socket_path
        self.pid_file = self.config.daemon_pid_path

        # Reactive state store
        self.store = DaemonStore()
        self.store.set_github_enabled(self.config.github_enabled)
        self._worktree_observer = None  # Filesystem watcher for worktrees dir

        # Managed state
        self.known_worktrees: dict[Path, DiscoveredWorktree] = {}
        self.worktree_index: WorktreeIndex | None = None
        self.gitstatusd_clients: dict[WorktreeID, GitstatusdListener] = {}
        self.git_watchers: dict[WorktreeID, DebouncedGitstatusRefresh] = {}

        # Centralized GitHub watcher (replaces per-worktree PRService)
        self.github_watcher: GitHubWatcher | None = None
        self._state_lock = asyncio.Lock()
        self.git_manager = GitManager(config=self.config)
        self.repo_status = RepoStatus(self.git_manager, self.config)
        self.worktree_service = WorktreeService(self.git_manager, self.github_interface)
        # Centralized git refs watcher for cached ahead/behind (created here, started in start())
        self.git_refs_watcher = GitRefsWatcher(store=self.store, git_manager=self.git_manager, config=self.config)
        # Build DI services
        self.index_service = WorktreeIndexService(
            get_index=lambda: self.worktree_index,
            rebuild_index=lambda: self.rebuild_index(),
            run_discovery_once=lambda: self._run_discovery_once(),
        )

        # Helper to fetch known worktree info without repeated membership checks
        def get_known_worktree(p):
            return self.known_worktrees.get(p)

        self.gitstatusd_service = GitstatusdService(
            get_client=lambda p: (self.gitstatusd_clients.get(wt.wtid) if (wt := get_known_worktree(p)) else None),
            iter_client_paths=lambda: list(self.known_worktrees.keys()),
            ensure_watcher_for_path=lambda p: (
                self._ensure_git_watcher(get_known_worktree(p)) if get_known_worktree(p) else asyncio.sleep(0)
            ),
            list_watchers=lambda: list(self.git_watchers.values()),
            clear_watchers=lambda: self.git_watchers.clear(),
        )
        self.discovery_service = DiscoveryService(
            lambda: self.discovery_scanning,
            periodic=lambda: self._periodic_discovery_wrapper(),
            cancel_periodic=lambda: self._cancel_periodic_discovery(),
        )
        # WtDaemon implements WorktreeCoordinator protocol directly

        # Server state
        self.server: asyncio.Server | None = None
        self.running = False
        self.discovery_task: asyncio.Task | None = None
        self.discovery_scanning: bool = False
        self.registry = WorktreeRegistry()
        self._startup_tasks: list[asyncio.Task] = []

        # Ensure daemon directory exists
        self.config.wt_dir.mkdir(exist_ok=True)

        # Defer initial discovery to start() to avoid running async in __init__
        self.known_worktrees = {}
        self.worktree_index = None

        self._method_handlers = rpc
        with contextlib.suppress(Exception):
            logger.info("Registered RPC methods: %s", sorted(rpc.list_methods()))

    def _validate_gitstatusd(self) -> tuple[str | None, str | None]:
        """Returns (gitstatusd_path, error_message) where error_message is None on success."""
        gitstatusd_path: str | None = None
        error: str | None = None

        if self.config.gitstatusd_path:
            # Prefer explicit configuration; do not fall back to PATH when set
            path = self.config.gitstatusd_path
            try:
                result = subprocess.run([path, "--version"], check=False, capture_output=True, timeout=2)
                if result.returncode == 0:
                    logger.info("Using configured gitstatusd at: %s", path)
                    gitstatusd_path = str(path)
                else:
                    error = f"Configured gitstatusd path not working: {path} (exit code {result.returncode})"
            except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
                error = f"Configured gitstatusd path failed: {path} ({e})"
        else:
            # Only check PATH - no hardcoded locations
            cmd = "gitstatusd"
            if shutil.which(cmd):
                try:
                    result = subprocess.run([cmd, "--version"], check=False, capture_output=True, timeout=2)
                    if result.returncode == 0:
                        logger.info("Found gitstatusd on PATH: %s", cmd)
                        gitstatusd_path = cmd
                    else:
                        error = f"gitstatusd found on PATH but not working (exit code {result.returncode})"
                except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
                    error = f"gitstatusd found on PATH but failed to execute: {e}"
            else:
                error = (
                    "gitstatusd binary not found. Please install gitstatusd and ensure it's available on PATH, "
                    "or configure gitstatusd_path in your config file. "
                    "Common installation: brew install romkatv/gitstatus/gitstatus"
                )

        return gitstatusd_path, error

    def _find_gitstatusd(self) -> str | None:
        gitstatusd_path, error = self._validate_gitstatusd()
        if error:
            logger.error(error)
        return gitstatusd_path

    def _validate_configuration(self) -> str | None:
        """Returns error message if configuration is invalid, None if valid."""
        errors = []

        # Check required paths exist
        if not self.config.main_repo.exists() or not self.config.main_repo.is_dir():
            errors.append(f"Main repository is not a directory: {self.config.main_repo}")

        # Check if main repo is actually a git repository
        git_dir = self.config.main_repo / ".git"
        if not git_dir.exists():
            errors.append(f"Main repository is not a git repository (no .git directory): {self.config.main_repo}")

        # Check worktrees directory can be created
        worktrees_dir = self.config.worktrees_dir
        if worktrees_dir.exists() and not worktrees_dir.is_dir():
            errors.append(f"Worktrees directory path exists but is not a directory: {worktrees_dir}")

        # Check daemon directory permissions
        try:
            self.config.wt_dir.mkdir(exist_ok=True)
        except PermissionError:
            errors.append(f"Cannot create daemon directory (permission denied): {self.config.wt_dir}")
        except OSError as e:
            errors.append(f"Cannot create daemon directory: {self.config.wt_dir} ({e})")

        # Validate GitHub configuration if enabled
        if self.config.github_enabled and not self.config.github_repo:
            errors.append("GitHub is enabled but github_repo is not configured")

        if errors:
            return "Configuration validation failed:\n" + "\n".join(f"  - {error}" for error in errors)

        return None

    async def _start_gitstatusd_for_worktree(self, worktree_info: DiscoveredWorktree) -> None:
        """Start gitstatusd for a worktree."""
        gitstatusd_path = self._find_gitstatusd()
        if not gitstatusd_path:
            logger.error("gitstatusd binary not found, cannot start process for %s", worktree_info.name)
            return

        if worktree_info.wtid in self.gitstatusd_clients:
            # Ensure watcher exists
            if worktree_info.wtid not in self.git_watchers:
                await self._ensure_git_watcher(worktree_info)
            return

        # Create listener - it owns its signal, no callbacks needed
        gs_client = GitstatusdListener(worktree_info.path, self.config, self.git_manager)
        await gs_client.start()
        # Register the listener's signal with the store
        self.store.register_gitstatusd(worktree_info.path, gs_client.status)
        # Kick an initial nonblocking refresh; watcher/poll keeps it fresh
        self._initial_status_task = asyncio.create_task(gs_client.update_working_status())
        self.gitstatusd_clients[worktree_info.wtid] = gs_client

        # Start .git watcher to drive status updates
        await self._ensure_git_watcher(worktree_info)

        # GitHubWatcher sees the new branch via store.active_branches
        if self.github_watcher:
            self.github_watcher.trigger_refresh()

        logger.info("Started gitstatusd for worktree %s", worktree_info.name)

    async def _stop_gitstatusd_for_worktree(self, worktree_info: DiscoveredWorktree) -> None:
        """Stop gitstatusd for a worktree."""
        gs_client = self.gitstatusd_clients.get(worktree_info.wtid)
        if gs_client:
            # Unregister the signal before stopping
            self.store.unregister_gitstatusd(worktree_info.path)
            await gs_client.stop()
            del self.gitstatusd_clients[worktree_info.wtid]
            logger.info("Stopped gitstatusd for worktree %s", worktree_info.name)
        watcher = self.git_watchers.get(worktree_info.wtid)
        if watcher:
            await watcher.stop()
            del self.git_watchers[worktree_info.wtid]

    async def _run_discovery_once(self) -> None:
        self.discovery_scanning = True
        try:
            current = await scan_worktrees(self.config.worktrees_dir)
            changes = self.registry.apply(current)
            async with self._state_lock:
                self.known_worktrees = dict(self.registry.known)
                self.worktree_index = WorktreeIndex.build(self.known_worktrees.values(), self.config.main_repo)
                # Sync paths to reactive store
                self.store.set_worktree_paths(self.known_worktrees.keys())
        finally:
            self.discovery_scanning = False
        for wt in changes.added:
            await self._start_gitstatusd_for_worktree(wt)
        for wt in changes.removed:
            await self._stop_gitstatusd_for_worktree(wt)

    async def _periodic_discovery(self) -> None:
        while self.running:
            try:
                await self._run_discovery_once()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic discovery")
                await asyncio.sleep(30)

    async def _periodic_discovery_wrapper(self) -> None:
        if self.discovery_task and not self.discovery_task.done():
            return
        self.discovery_task = asyncio.create_task(self._periodic_discovery())

    def _cancel_periodic_discovery(self) -> None:
        if self.discovery_task:
            self.discovery_task.cancel()

    async def handle_client_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a client request using JSON-RPC 2.0 protocol."""
        start_time = datetime.now()

        try:
            # Read request line
            if not (data := await reader.readline()):
                return

            # Parse JSON-RPC request
            request = parse_request(data.decode().strip())
            request_id = request.id
            method = request.method
            logger.info("Handling JSON-RPC request %s: %s", request_id, method)

            # Kick discovery opportunistically but do not block request handling
            if not self.known_worktrees and not self.discovery_scanning:
                self._discovery_kick = asyncio.create_task(self._run_discovery_once())

            # Handle request via RPC registry only
            response = await self._method_handlers.dispatch(request, self, writer, start_time)
            await self._send_response(writer, response)
            return

        except Exception:
            logger.exception("Error handling client request")
            rid = request.id if ("request" in locals() and request) else uuid.UUID(int=0)
            with contextlib.suppress(Exception):
                await self._send_response(
                    writer, create_error_response(ErrorCodes.INTERNAL_ERROR, "Internal server error", rid)
                )
        finally:
            writer.close()
            await writer.wait_closed()

    def _create_success_response(self, result: Any, request_id: uuid.UUID) -> Response:
        """Create a successful JSON-RPC response."""
        return Response(result=result, id=request_id)

    async def _send_response(self, writer: asyncio.StreamWriter, response: Response | ErrorResponse) -> None:
        """Send a JSON-RPC response to the client."""
        response_data = response.model_dump_json().encode()
        writer.write(response_data)
        writer.write(b"\n")
        await writer.drain()

    async def _handle_ping_request(self, request: Request, start_time: datetime) -> Response:
        """Handle ping JSON-RPC method."""
        result = PingResult(
            daemon_pid=os.getpid(), started_at=start_time, discovered_worktrees=list(self.known_worktrees.keys())
        )
        return self._create_success_response(result, request.id)

    async def _handle_shutdown_request(self, request: Request, start_time: datetime | None = None) -> Response:
        """Handle shutdown JSON-RPC method."""
        logger.info("Received shutdown request")
        self._shutdown_task = asyncio.create_task(self.stop())
        return self._create_success_response("shutting down", request.id)

    async def _ensure_git_watcher(self, worktree_info: DiscoveredWorktree) -> None:
        if worktree_info.wtid in self.git_watchers:
            return
        gs_client = self.gitstatusd_clients.get(worktree_info.wtid)
        if not gs_client:
            return

        async def _cb(reason: str):
            await gs_client.update_working_status()

        watcher = DebouncedGitstatusRefresh(
            worktree_path=worktree_info.path,
            refresh_callback=_cb,
            debounce_delay=self.config.git_watcher_debounce_delay.total_seconds(),
        )
        await watcher.start()
        self.git_watchers[worktree_info.wtid] = watcher

    async def start(self) -> None:
        """Start the daemon."""
        logger.info("Starting wt daemon for %s", self.config.main_repo)

        # Emit initial progress handshake to ensure the client always sees at least one line
        write_startup_handshake(success=True, protocol_version=1, ready=False, phase="starting", redirect_after=False)

        startup_errors = []

        # Validate configuration first
        config_error = self._validate_configuration()
        if config_error:
            startup_errors.append(config_error)

        # Post-creation script is validated at use-time in WorktreeService

        # Validate gitstatusd availability
        gitstatusd_path, gitstatusd_error = self._validate_gitstatusd()
        if gitstatusd_error:
            startup_errors.append(gitstatusd_error)
            self.store.set_gitstatusd_config(GitstatusdUnavailable(error=gitstatusd_error))
        elif gitstatusd_path:
            self.store.set_gitstatusd_config(GitstatusdAvailable(path=gitstatusd_path))

        # If there are critical errors, write error handshake and return
        if startup_errors:
            error_message = "\n\n".join(startup_errors)
            write_startup_handshake(
                success=False,
                error_message=error_message,
                protocol_version=1,
                daemon_log_path=self.config.daemon_log_file,
            )
            logger.error("Daemon startup failed due to validation errors")
            return

        # Bind socket immediately after validation
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
            self.server = await asyncio.start_unix_server(self.handle_client_request, self.socket_path)
            # Write PID file in thread to avoid blocking the event loop
            await asyncio.to_thread(self.pid_file.write_text, str(os.getpid()))
            self.running = True
        except OSError as e:
            # Emit failure handshake so client can surface the error deterministically
            write_startup_handshake(
                success=False,
                error_message=f"Failed to bind daemon socket at {self.socket_path}: {e}",
                protocol_version=1,
                daemon_log_path=self.config.daemon_log_file,
            )
            logger.error("Socket bind failed: %s", e)
            # Ensure clean shutdown of any partial state
            with contextlib.suppress(Exception):
                await self.stop()
            return

        # Signal listening via single handshake; redirect stdout to log afterward
        write_startup_handshake(
            success=True,
            protocol_version=1,
            ready=True,
            gitstatusd_path=gitstatusd_path,
            discovered_worktrees=[],
            socket_path=str(self.socket_path),
            redirect_after=True,
            daemon_log_path=self.config.daemon_log_file,
        )

        logger.info("wt daemon started, listening on %s", self.socket_path)

        # Start centralized GitHub watcher
        self.github_watcher = GitHubWatcher(
            branches_signal=self.store.active_branches, github_interface=self.github_interface, config=self.config
        )
        await self.github_watcher.start()

        # Start centralized git refs watcher for cached ahead/behind
        await self.git_refs_watcher.start()

        # Start long-running service loops
        await self.discovery_service.start()
        await self.gitstatusd_service.start()

        # Start filesystem watcher for worktrees directory
        if not self.config.worktrees_dir.exists():
            raise RuntimeError(f"Worktrees directory does not exist: {self.config.worktrees_dir}")
        self._worktree_observer = start_watcher(self.store, self.config.worktrees_dir)

        self._discovery_kick = asyncio.create_task(self._run_discovery_once())

    async def stop(self) -> None:
        """Stop the daemon."""
        logger.info("Stopping wt daemon")

        self.running = False

        # Stop filesystem watcher
        if self._worktree_observer:
            self._worktree_observer.stop()
            self._worktree_observer.join(timeout=2.0)
            self._worktree_observer = None

        # Stop long-running service loops
        await self.gitstatusd_service.stop()
        if self.github_watcher:
            await self.github_watcher.stop()
        await self.git_refs_watcher.stop()
        await self.discovery_service.stop()

        # Stop all gitstatusd processes
        for process in list(self.gitstatusd_clients.values()):
            await process.stop()
        self.gitstatusd_clients.clear()

        # Stop server
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        # Clean up files
        if self.socket_path.exists():
            self.socket_path.unlink()
        if self.pid_file.exists():
            self.pid_file.unlink()

        logger.info("wt daemon stopped")

    async def rebuild_index(self) -> None:
        async with self._state_lock:
            self.worktree_index = WorktreeIndex.build(self.known_worktrees.values(), self.config.main_repo)

    async def register_worktree(self, info: DiscoveredWorktree) -> None:
        async with self._state_lock:
            self.known_worktrees[info.path] = info
        await self._start_gitstatusd_for_worktree(info)
        await self.rebuild_index()

    async def unregister_worktree(self, info: DiscoveredWorktree) -> None:
        await self._stop_gitstatusd_for_worktree(info)
        async with self._state_lock:
            self.known_worktrees.pop(info.path, None)
        await self.rebuild_index()


async def run_daemon(config) -> None:
    """Run the daemon with proper signal handling."""
    daemon = WtDaemon(config)

    # Signal handling
    def signal_handler():
        logger.info("Received shutdown signal")
        daemon._shutdown_task = asyncio.create_task(daemon.stop())

    signal.signal(signal.SIGTERM, lambda s, f: signal_handler())
    signal.signal(signal.SIGINT, lambda s, f: signal_handler())

    try:
        await daemon.start()

        # Wait for server
        if daemon.server:
            async with daemon.server:
                await daemon.server.serve_forever()

    except asyncio.CancelledError:
        pass
    finally:
        await daemon.stop()


if __name__ == "__main__":
    # Load config using the standard discovery system
    config = load_config()

    # Configure logging to write only to daemon log file
    config.wt_dir.mkdir(exist_ok=True)
    log_file = config.daemon_log_file

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file, mode="a")
            # No StreamHandler - daemon should not output to console
        ],
    )

    # Also capture urllib3 and other third-party debug logs
    # This ensures ALL logging goes to the file, not the console
    urllib3_logger = logging.getLogger("urllib3")
    urllib3_logger.setLevel(logging.DEBUG)
    urllib3_logger.propagate = True  # Ensure it propagates to our file handler

    asyncio.run(run_daemon(config))
