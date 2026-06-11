"""Containerized Claude interface providing ClaudeSDKClient compatibility.

# DEBUGGING REFERENCE

For comprehensive debugging information about containerized Claude execution,
including critical issues and solutions, see:

    DEBUGGING.md

This file contains detailed documentation about:
- Docker buildx context isolation issues
- Container user permission problems
- User-specified global pre-task setup
- File exclusion for grader
- Debugging commands and common failure modes

Preserve DEBUGGING.md - it will save hours when things break.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import traceback
from collections.abc import AsyncIterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import claude_code_sdk
import docker
import pathspec
from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
from claude_code_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from docker.models.containers import Container

from inop.engine.models import SeedTask


@contextmanager
def temporary_env_var(key: str, value: str):
    prev = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


@contextmanager
def temporary_env(overrides: dict[str, str]):
    with ExitStack() as stack:
        for k, v in overrides.items():
            stack.enter_context(temporary_env_var(k, v))
        yield


@dataclass
class ContainerizedClaudeCodeOptions(ClaudeCodeOptions):
    """ClaudeCodeOptions with containerization support."""

    claude_binary: str | None = None


def _monkey_patch_claude_sdk_for_containerization():
    """Monkeypatch Claude SDK to use our containerized wrapper instead of PATH lookup.

    Injects claude_binary option to override hardcoded PATH lookup.
    See DEBUGGING.md for implementation details and failure modes.
    """
    # Override _find_cli to use claude_binary from options
    # Replace the options class so we can pass claude_binary
    claude_code_sdk.types.ClaudeCodeOptions = ContainerizedClaudeCodeOptions  # type: ignore[misc]
    claude_code_sdk.ClaudeCodeOptions = ContainerizedClaudeCodeOptions  # type: ignore[misc]

    def _patched_find_cli(self) -> str:
        if not isinstance(self._options, ContainerizedClaudeCodeOptions):
            raise RuntimeError("No claude_binary in options")
        bin_path = self._options.claude_binary
        if not bin_path:
            raise RuntimeError("claude_binary not set in options")
        return bin_path

    SubprocessCLITransport._find_cli = _patched_find_cli  # type: ignore[method-assign]


# Apply monkeypatch once at module level
_monkey_patch_claude_sdk_for_containerization()


class TaskClaude:
    """Containerized Claude client with ClaudeSDKClient-compatible interface.

    Provides the same API as ClaudeSDKClient but runs Claude inside Docker containers
    with proper PATH isolation and automatic file collection.
    """

    # TODO/TBD: Git volume was a historical optimization for pre-cloned large repositories.
    # With shallow clones (git fetch --depth 1) to specific commits, this optimization
    # may no longer be necessary. The git volume code is kept for potential future use
    # but is DISABLED in the production path.
    #
    # Historical context:
    # - Git repos were pre-cloned into a Docker volume named "claude_shared_git"
    # - Volume was mounted at /git (read-write initially, then remounted read-only)
    # - This avoided repeated cloning of large repos across tasks
    #
    # Current approach:
    # - Clone directly into workspace for each task
    # - No shared git volume needed
    # - Simpler and more consistent with OpenAIRunner
    #
    # To re-enable git volume: set use_git_volume = True
    use_git_volume = False  # DISABLED in production path

    def __init__(self, task_id: str, config, output_dir: Path, seed_task: SeedTask, logger):
        self.task_id = task_id
        self.config = config
        self._output_dir = output_dir
        self._task = seed_task.prompt
        self._seed_task = seed_task
        self._docker_client = docker.from_env()
        self._container: Container | None = None
        self._message_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._query_task = None
        self._docker_image: str | None = seed_task.docker_image
        self._git_volume_name = "claude_shared_git"
        self._logger = logger
        self._exclusion_spec = pathspec.PathSpec.from_lines("gitwildmatch", config.get("exclude_patterns", []))

        # Find docker path for wrapper creation
        docker_path = shutil.which("docker")
        if not docker_path:
            raise RuntimeError("Docker binary not found in PATH")
        self._docker_path: str = docker_path

    @property
    def container_id(self) -> str:
        """Get container ID for external operations."""
        c = self._container_or_raise()
        return str(c.id)

    def _ensure_container_ready(self):
        """Runtime safety check - call before any container operations."""
        if not self._container:
            raise RuntimeError("Container not started - TaskClaude must be used as context manager")

    def _container_or_raise(self) -> Container:
        c = self._container
        if c is None:
            raise RuntimeError("Container not started")
        if c.id is None:
            raise RuntimeError("Container created but has no ID")
        return c

    def _get_container_volumes(self, git_readonly: bool) -> dict:
        """Get volume configuration for container with persistent logging.

        Ensures git volume exists (if enabled) and returns complete volume configuration.
        """
        # Create output directory if it doesn't exist
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Ensure logs directory exists on host
        logs_dir = self._output_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        volumes = {
            str(self._output_dir): {"bind": "/workspace", "mode": "rw"},
            str(logs_dir): {"bind": "/logs", "mode": "rw"},  # Persistent logs survive container death
        }

        # Only include git volume if enabled (currently disabled in production)
        if self.use_git_volume:
            # Ensure shared git volume exists
            try:
                self._docker_client.volumes.get(self._git_volume_name)
            except docker.errors.NotFound:
                self._docker_client.volumes.create(name=self._git_volume_name)

            git_mode = "ro" if git_readonly else "rw"
            volumes[self._git_volume_name] = {"bind": "/git", "mode": git_mode}

        return volumes

    def _create_container(self, volumes: dict) -> Container:
        """Create Docker container with standardized configuration.

        Centralizes container creation to eliminate duplication and ensure
        consistent settings across initial and remounted containers.
        """
        if self._docker_image is None:
            raise RuntimeError("Docker image not set")
        return self._docker_client.containers.create(
            self._docker_image,
            command=["/usr/bin/tini", "--", "sleep", "infinity"],  # Use tini as PID 1 to reap zombies
            entrypoint=[],  # Clear any inherited entrypoint from base image
            volumes=volumes,
            working_dir="/workspace",
        )

    async def query(self) -> None:
        """Prepare containerized Claude query (setup options)."""
        self._ensure_container_ready()

        # Validate running status
        try:
            c = self._container_or_raise()
            c.reload()
            if c.status != "running":
                logs = c.logs().decode("utf-8", errors="replace")
                self._logger.error("Container not running - logs:", container_id=c.id, status=c.status, logs=logs)
                raise RuntimeError(f"Container {c.id} is not running, status: {c.status}")
        except Exception as e:
            cid = self._container.id if self._container else "<none>"
            raise RuntimeError(f"Container {cid} check failed: {e}") from e

        c = self._container_or_raise()
        self._logger.info("Query starting", container_id=c.id, container_status=c.status)

        options = ContainerizedClaudeCodeOptions(
            allowed_tools=[],
            cwd=str(self._output_dir),
            max_turns=self.config.get("max_turns", 30),
            permission_mode="bypassPermissions",
            mcp_servers={},
            claude_binary=self._get_docker_exec_wrapper_path(),
        )

        self._claude_options = options

    async def receive_messages(self) -> AsyncIterator[Any]:
        """Receive messages from Claude - matches ClaudeSDKClient.receive_messages()."""
        self._ensure_container_ready()

        # Set up environment variables for wrapper script execution
        c = self._container_or_raise()
        wrapper_env: dict[str, str] = {"CLAUDE_CONTAINER_ID": str(c.id), "DOCKER_BINARY": self._docker_path}

        if self.config.get("enable_strace", False):
            wrapper_env["CLAUDE_STRACE"] = "1"
        if self.config.get("wrapper_env"):
            wrapper_env.update(self.config.get("wrapper_env"))
        pass_keys = [k for k in wrapper_env if k not in ("CLAUDE_CONTAINER_ID", "DOCKER_BINARY")]
        if pass_keys:
            wrapper_env["CLAUDE_WRAPPER_PASS_ENV"] = ",".join(pass_keys)

        with temporary_env(wrapper_env):
            async with ClaudeSDKClient(options=self._claude_options) as client:
                await client.query(self._task)
                async for message in client.receive_messages():
                    yield message

    def setup_system_prompt(self, system_prompt: str):
        """Write CLAUDE.md inside the container (call before PATH isolation).

        Writes system prompt to container /workspace via bind mount.
        Much simpler and safer than Docker API tar archives.
        """
        c = self._container_or_raise()

        self._logger.debug("Writing system prompt to container", container_id=c.id, prompt_length=len(system_prompt))

        # Write CLAUDE.md directly to bind-mounted directory
        claude_md_path = self._output_dir / "CLAUDE.md"
        claude_md_path.write_text(system_prompt, encoding="utf-8")

    async def collect_outputs(self) -> list[dict[str, str]]:
        self._ensure_container_ready()

        files: list[dict[str, str]] = []
        for file_path in self._output_dir.rglob("*"):
            if not file_path.is_file():
                continue

            relative_path = file_path.relative_to(self._output_dir).as_posix()
            if self._exclusion_spec.match_file(relative_path):
                continue

            # Try to read as text, skip binary files
            try:
                content = file_path.read_text(encoding="utf-8")
                files.append({"path": relative_path, "content": content})
            except UnicodeDecodeError:
                # Skip binary files
                self._logger.debug("Skipping binary file", path=relative_path, size=file_path.stat().st_size)
                continue

        return files

    def _setup_docker_image(self):
        """Validate docker image from task configuration."""
        if not self._docker_image:
            raise ValueError(f"Task '{self.task_id}' has no docker_image specified")
        self._logger.info("Using Docker image", docker_image=self._docker_image)

    def _get_docker_exec_wrapper_path(self) -> str:
        """Get docker exec wrapper script for Claude SDK.

        Returns path to committed wrapper script and sets required environment variables.
        The container has claude-wrapper that calls claude.
        """
        # Ensure container exists (for logging)
        c = self._container_or_raise()

        # Use committed wrapper script from repo
        wrapper_script = Path(__file__).parent.parent.parent.parent / "scripts" / "claude_docker_wrapper"
        if not wrapper_script.exists():
            raise RuntimeError(f"Claude docker wrapper script not found: {wrapper_script}")

        self._logger.debug("Using committed Claude docker wrapper", wrapper_path=str(wrapper_script), container_id=c.id)
        return str(wrapper_script)

    async def _start_container(self):
        """Start Docker container for the task.

        Multi-stage startup: initial container -> setup scripts -> remount -> wrapper.
        See DEBUGGING.md for detailed process and failure modes.
        """
        try:
            # Setup docker image from task configuration
            self._setup_docker_image()

            volumes = self._get_container_volumes(git_readonly=False)

            self._logger.info("Starting initial container", docker_image=self._docker_image, volumes=volumes)
            self._container = self._create_container(volumes)
            c = self._container_or_raise()
            c.start()

            self._logger.info("Waiting for running status", container_id=c.id, status=c.status)
            max_wait = 30  # seconds
            for _i in range(max_wait):
                c.reload()
                if c.status == "running":
                    break
                if c.status in ["exited", "dead"]:
                    logs = c.logs().decode("utf-8", errors="replace")
                    self._logger.error(
                        "Container died during startup",
                        container_id=c.id,
                        status=c.status,
                        logs=logs,
                        debug_hint=f"Run: docker logs {c.id}",
                    )
                    raise RuntimeError(f"Container {c.id} died during startup: {c.status}")

                await asyncio.sleep(1)

            if c.status != "running":
                logs = c.logs().decode("utf-8", errors="replace")
                self._logger.error(
                    "Container failed to start within timeout",
                    container_id=c.id,
                    status=c.status,
                    timeout=max_wait,
                    logs=logs,
                    debug_hint=f"Run: docker logs {c.id}",
                )

                raise RuntimeError(f"Container {c.id} failed to start within {max_wait}s: {c.status}")

            self._logger.info("Initial container running", container_id=c.id, status=c.status)

            # ALWAYS remount git as read-only for security (do this BEFORE setup scripts)
            await self._remount_git_readonly()

            # Run pre-task setup after remount so installs persist
            if self.config.get("pre_task_always_script"):
                await self._run_pre_task_always_setup()

            if self.config.get("pre_task_setup_script"):
                await self._run_pre_task_setup()

            if self._seed_task.pre_task_commands:
                await self._run_pre_task_commands(self._seed_task.pre_task_commands)

        except Exception as e:
            if self._container:
                self._logger.error(
                    "Container setup failed - CONTAINER LEFT RUNNING FOR DEBUG",
                    container_id=self._container.id,
                    error=str(e),
                    debug_hint=f"Run: docker logs {self._container.id}",
                )
                # Don't cleanup container on error - leave running for debugging (see DEBUGGING.md)
                self._container = None  # Prevent cleanup in __aexit__
            raise

    async def _run_setup_script(self, script_path: str, script_type: str, log_prefix: str):
        """Run a setup script with streaming output and error handling."""
        setup_script = Path(script_path)
        if not setup_script.exists():
            raise FileNotFoundError(f"{script_type} script not found: {setup_script}")

        # Debug: Log the exact command being executed
        c = self._container_or_raise()
        cmd_args = [str(setup_script), c.id, self.task_id, str(self._output_dir)]
        self._logger.info(
            f"Running {script_type.lower()} script",
            script=str(setup_script),
            container_id=c.id,
            task_id=self.task_id,
            cmd_args=cmd_args,
            script_permissions=oct(setup_script.stat().st_mode),
            script_size=setup_script.stat().st_size,
        )

        process = await asyncio.create_subprocess_exec(
            str(setup_script),
            str(c.id),
            self.task_id,
            str(self._output_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout for simpler streaming
        )

        # Stream output in real-time
        if process.stdout is None:
            raise RuntimeError("Setup script process has no stdout pipe")
        while line := await process.stdout.readline():
            try:
                line_text = line.decode("utf-8", errors="replace").rstrip()
                if line_text:  # Only log non-empty lines
                    self._logger.info(log_prefix.lower(), container_id=c.id, output=line_text)
            except UnicodeDecodeError as e:
                stack_trace = traceback.format_exc()
                self._logger.error(
                    "Unicode decode error in setup script output",
                    error=str(e),
                    line_bytes=line.hex() if line else "None",
                    container_id=c.id,
                    stack_trace=stack_trace,
                )
                # Continue processing despite decode error
                continue

        exit_code = await process.wait()

        if exit_code != 0:
            self._logger.error(
                f"{script_type} script failed - CONTAINER LEFT RUNNING FOR DEBUG",
                container_id=c.id,
                exit_code=exit_code,
                debug_hint=f"Run: docker logs {c.id}",
            )
            raise RuntimeError(f"{script_type} script failed with exit code {exit_code}")
        self._logger.info(f"{script_type} script completed successfully", container_id=c.id)

    async def _run_pre_task_always_setup(self):
        """Run always pre-task setup script (runs before every task)."""
        if not self.config.get("pre_task_always_script"):
            return
        await self._run_setup_script(self.config.get("pre_task_always_script"), "Always pre-task setup", "setup-always")

    async def _run_pre_task_setup(self):
        """Run pre-task setup script."""
        if not self.config.get("pre_task_setup_script"):
            return
        await self._run_setup_script(self.config.get("pre_task_setup_script"), "Pre-task setup", "pre-task-setup")

    async def _run_pre_task_commands(self, commands: str):
        """Run pre-task commands inside the container."""
        if not commands or not commands.strip():
            return

        c = self._container_or_raise()
        self._logger.info("Running pre-task commands", container_id=c.id, commands_preview=commands[:100])

        # Execute commands inside container using docker exec
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            str(c.id),
            "/bin/bash",
            "-c",
            commands,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream output in real-time
        if process.stdout is None:
            raise RuntimeError("Pre-task commands process has no stdout pipe")
        while True:
            try:
                line = await process.stdout.readline()
                if not line:
                    break
                line_text = line.decode("utf-8", errors="replace").rstrip()
                if line_text:
                    self._logger.info("pre-task-cmd", container_id=c.id, output=line_text)
            except UnicodeDecodeError as e:
                self._logger.error("Unicode decode error in pre-task commands output", error=str(e), container_id=c.id)
                continue

        exit_code = await process.wait()

        if exit_code != 0:
            self._logger.error("Pre-task commands failed", container_id=c.id, exit_code=exit_code)
            raise RuntimeError(f"Pre-task commands failed with exit code {exit_code}")
        self._logger.info("Pre-task commands completed successfully", container_id=c.id)

    async def _remount_git_readonly(self):
        """Remount git volume as read-only for security.

        Restarts container with RO git mount. Wrapper script recreated with new container ID.
        See DEBUGGING.md for security rationale and troubleshooting.

        NOTE: This is only needed when git volume is enabled. Currently skipped in production.
        """
        if not self.use_git_volume:
            # Git volume disabled - no remounting needed
            self._logger.info("Git volume disabled - skipping remount")
            # Still need to setup the wrapper after container start
            await self._setup_wrapper()
            return

        # Stop current container
        old_container_id = self._container_or_raise().id
        self._logger.info("Remounting git volume as read-only", old_container_id=old_container_id)

        self._container_or_raise().remove(force=True)

        # Start new container with RO git mount and persistent logs
        volumes = self._get_container_volumes(git_readonly=True)

        self._logger.info("Starting remounted container", docker_image=self._docker_image, volumes=volumes)

        self._container = self._create_container(volumes)
        self._container.start()

        self._logger.info(
            "Remounted container created - waiting for running status",
            new_container_id=self._container_or_raise().id,
            old_container_id=old_container_id,
            status=self._container_or_raise().status,
        )

        # Wait for remounted container to actually start running
        max_wait = 30  # seconds
        for _i in range(max_wait):
            c = self._container_or_raise()
            c.reload()
            if c.status == "running":
                break
            if c.status in ["exited", "dead"]:
                logs = c.logs().decode("utf-8", errors="replace")
                self._logger.error(
                    "Remounted container died during startup",
                    container_id=c.id,
                    status=c.status,
                    logs=logs,
                    debug_hint=f"Run: docker logs {c.id}",
                )
                raise RuntimeError(f"Remounted container {c.id} died during startup: {c.status}")

            await asyncio.sleep(1)

        c = self._container_or_raise()
        if c.status != "running":
            logs = c.logs().decode("utf-8", errors="replace")
            self._logger.error(
                "Remounted container failed to start within timeout",
                container_id=c.id,
                status=c.status,
                timeout=max_wait,
                logs=logs,
                debug_hint=f"Run: docker logs {c.id}",
            )
            raise RuntimeError(f"Remounted container {c.id} failed to start within {max_wait}s: {c.status}")

        self._logger.info("Remounted container running", container_id=c.id, status=c.status)

    async def _setup_wrapper(self) -> None:
        """Set up or refresh any container-dependent wrapper state.

        Currently a no-op: we use a committed host-side wrapper script that only
        needs the container ID and docker binary provided via environment in
        receive_messages(). This hook is kept for future extensibility.
        """
        return

    async def _cleanup(self):
        """Clean up container and wrapper."""

        if self._container:
            try:
                c = self._container
                c.remove(force=True)
            except Exception as e:
                cid = "<none>"
                with suppress(Exception):
                    cid = str(self._container_or_raise().id)
                raise RuntimeError(f"Failed to remove container {cid}: {e}") from e
            finally:
                self._container = None

    async def __aenter__(self) -> TaskClaude:
        """Context manager entry - setup container and wrapper."""
        # Start container (includes wrapper setup after remounting)
        await self._start_container()

        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb):
        """Context manager exit - cleanup container and wrapper."""
        # Stop query task if running
        if self._query_task and not self._query_task.done():
            self._query_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._query_task

        # Cleanup container and wrapper
        try:
            await self._cleanup()
        except Exception as e:
            self._logger.error(
                "Error during cleanup", error=str(e), container_id=self._container.id if self._container else None
            )
