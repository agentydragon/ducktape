"""Worktree operations orchestrator layer that performs I/O and subprocess work."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import psutil
import pygit2

from ..shared.configuration import Configuration
from ..shared.error_handling import ErrorContext, validate_worktree_name

# PR types are referenced by protocol layer; not needed here directly
from ..shared.models import ProcessInfo
from ..shared.protocol import WorktreeID
from .copy_strategies import get_copy_strategy
from .git_manager import GitManager
from .github_client import GitHubInterface
from .worktree_ids import wtid_to_path

logger = logging.getLogger(__name__)


class WorktreeService:
    """Worktree operations orchestrator.

    Responsibilities:
    - Validate names and configuration
    - Create branches and add worktrees (delegates to GitManager)
    - Optional hydration (copy or checkout) per configuration
    - Run post-creation hooks with streaming sink
    - Query processes that hold files in a worktree

    Note: This layer performs filesystem and subprocess I/O.
    """

    def __init__(self, git_manager: GitManager, github: GitHubInterface | None):
        self.git_manager = git_manager
        self.github = github

    def list_worktrees(self, config: Configuration) -> list[tuple[str, Path, bool]]:
        """List all managed worktrees with their existence status."""
        worktree_infos = self.git_manager.list_worktrees()
        worktrees = []

        for info in worktree_infos:
            if self._is_managed_worktree(info.path, config) and not info.is_main:
                worktrees.append((info.path.name, info.path, info.exists))

        return worktrees

    def _is_managed_worktree(self, path: Path, config: Configuration) -> bool:
        """Check if this worktree should be managed by our tool."""
        # Skip the main repo
        if path.resolve() == config.main_repo.resolve():
            return False

        # Only include worktrees in our managed directory
        if not path.is_relative_to(config.worktrees_dir):
            return False

        # Filter out hidden worktrees using configurable patterns
        return not any(path.name.startswith(pattern) for pattern in config.hidden_worktree_patterns)

    def _require_post_creation_script_valid(self, config: Configuration) -> None:
        if config.post_creation_script:
            script = config.post_creation_script
            if not script.exists() or not script.is_file():
                raise FileNotFoundError(f"Post-creation script {script} is not a file")

    def _wtid_to_path(self, config: Configuration, wtid: WorktreeID) -> Path:
        return wtid_to_path(config, wtid)

    def create_worktree(
        self, config: Configuration, name: str, source_worktree: Path | None = None, source_branch: str | None = None
    ) -> Path:
        """Create a new worktree."""
        validate_worktree_name(name)
        self._require_post_creation_script_valid(config)
        worktree_path: Path = config.worktrees_dir / name

        if worktree_path.exists():
            raise RuntimeError(f"Worktree '{name}' already exists at {worktree_path}")

        # Ensure worktrees directory exists
        config.worktrees_dir.mkdir(parents=True, exist_ok=True)

        with ErrorContext("create_worktree", name):
            branch_name = f"{config.branch_prefix}{name}"

            # Use provided source_branch if given; otherwise configured upstream
            self.git_manager.create_branch(branch_name, source_branch or config.upstream_branch)

            # Create worktree
            self.git_manager.worktree_add(worktree_path, branch_name)

            # Hydrate with dirty state if source provided
            if config.hydrate_worktrees:
                if source_worktree:
                    logger.info(f"Hydrating new worktree in {worktree_path} from {source_worktree}.")
                    if not source_worktree.exists():
                        raise RuntimeError(f"Source worktree does not exist: {source_worktree}")
                    self._hydrate_worktree(config, source_worktree, worktree_path)
                else:
                    logger.info(f"Hydrating new worktree in {worktree_path} by checking out {branch_name}.")
                    repo = pygit2.Repository(worktree_path)
                    repo.set_head(f"refs/heads/{branch_name}")
                    repo.checkout_head(strategy=pygit2.GIT_CHECKOUT_FORCE)
            else:
                logger.info("Not hydrating worktree.")
            return worktree_path

    def get_worktree_path(self, config: Configuration, name: str) -> Path:
        """Get path for a worktree by name."""
        return config.worktrees_dir / name

    async def remove_worktree(self, config: Configuration, name: str, force: bool = False) -> None:
        """Remove a worktree by name and clean up its directory."""
        validate_worktree_name(name)
        worktree_path = self.get_worktree_path(config, name)
        if not worktree_path.exists():
            return
        self.git_manager.worktree_remove(worktree_path, force=force)
        with contextlib.suppress(Exception):
            shutil.rmtree(worktree_path, ignore_errors=True)

    def require_worktree_exists(self, config: Configuration, name: str) -> Path:
        """Require that a worktree exists and return its path."""
        worktree_path = self.get_worktree_path(config, name)
        if not worktree_path.exists():
            raise RuntimeError(f"Worktree '{name}' does not exist")
        return worktree_path

    def _hydrate_worktree(self, config: Configuration, src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        get_copy_strategy(config.cow_method).copy(src, dst)

    @staticmethod
    async def run_post_creation_script(
        script_path: str,
        worktree_path: Path,
        sink: (Callable[[str, str], Awaitable[None]] | Callable[[str, str], None] | None) = None,
        deadline: float = 60.0,
    ) -> dict:
        script = Path(script_path).expanduser().resolve()
        if not script.exists() or not script.is_file():
            return {
                "ran": False,
                "exit_code": None,
                "stdout": None,
                "stderr": None,
                "error": "not_found" if not script.exists() else "not_file",
            }

        proc = await asyncio.create_subprocess_exec(
            script,
            f"--worktree_root={worktree_path}",
            f"--worktree_name={worktree_path.name}",
            cwd=worktree_path,
            stdin=asyncio.subprocess.DEVNULL,  # ensure valid fd 0 for hook
            # avoids CPython init_sys_streams crashes if parent stdin is closed
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if sink is None:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=deadline)
                return {
                    "ran": True,
                    "exit_code": proc.returncode,
                    "stdout": stdout.decode(errors="replace") if stdout else None,
                    "stderr": stderr.decode(errors="replace") if stderr else None,
                    "error": None,
                }
            except TimeoutError:
                with contextlib.suppress(Exception):
                    proc.kill()
                    await proc.wait()
                return {
                    "ran": True,
                    "exit_code": None,
                    "stdout": None,
                    "stderr": None,
                    "error": "timeout",
                    "timeout_secs": float(deadline),
                }

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        async def _forward(stream, name):  # Streams stdout/stderr; expensive O(stream size)
            chunk_size_bytes = 4096
            while True:
                data = await stream.read(chunk_size_bytes)
                if not data:
                    break
                text = data.decode(errors="replace")
                if name == "stdout":
                    stdout_buf.append(text)
                else:
                    stderr_buf.append(text)
                try:
                    result = sink(name, text)
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.debug("hook sink failed", exc_info=True)

        t1 = asyncio.create_task(_forward(proc.stdout, "stdout")) if proc.stdout else None
        t2 = asyncio.create_task(_forward(proc.stderr, "stderr")) if proc.stderr else None
        try:
            await asyncio.wait_for(proc.wait(), timeout=deadline)
        except TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()
            return {
                "ran": True,
                "exit_code": None,
                "stdout": None,
                "stderr": None,
                "error": "timeout",
                "timeout_secs": float(deadline),
            }
        if t1:
            await t1
        if t2:
            await t2
        # Include truncated previews to aid diagnostics while avoiding duplication in client
        preview_max = 8192
        out_text = "".join(stdout_buf)
        err_text = "".join(stderr_buf)
        out_preview = out_text[-preview_max:] if len(out_text) > preview_max else out_text
        err_preview = err_text[-preview_max:] if len(err_text) > preview_max else err_text
        return {
            "ran": True,
            "exit_code": proc.returncode,
            "stdout": out_preview or None,
            "stderr": err_preview or None,
            "error": None,
            "streamed": True,
        }

    def _get_processes_in_directory(self, directory: Path) -> list:
        """Get processes running in a directory.

        Note: O(size of process table) due to psutil.process_iter and open_files scanning.
        """
        procs = []
        for proc in psutil.process_iter(["pid", "name", "cwd"]):
            try:
                cwd = proc.info.get("cwd")
                if cwd and Path(cwd).is_relative_to(directory):
                    procs.append(ProcessInfo(pid=proc.pid, name=proc.name()))
                    continue
                for fl in proc.open_files():
                    if fl.path and Path(fl.path).is_relative_to(directory):
                        procs.append(ProcessInfo(pid=proc.pid, name=proc.name()))
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return procs
