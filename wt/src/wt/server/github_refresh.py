import asyncio
import logging
from pathlib import Path
import time
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class DebouncedGitHubRefresh:
    """Handles debounced GitHub refresh triggered by .git directory changes + periodic updates."""

    def __init__(
        self, worktree_path: Path, refresh_callback, debounce_delay: float = 5.0, periodic_interval: float = 60.0
    ):
        """Initialize DebouncedGitHubRefresh.

        Args:
            worktree_path: Path to the worktree directory to watch.
            refresh_callback: Async callback invoked as refresh_callback(reason: str, files_changed: list[str]).
            debounce_delay: Seconds to wait after filesystem events before triggering a refresh.
            periodic_interval: Seconds between periodic refresh attempts.
        """
        self.worktree_path = worktree_path
        self.refresh_callback = refresh_callback
        self.debounce_delay = debounce_delay
        self.periodic_interval = periodic_interval
        self.pending_refresh_task: asyncio.Task | None = None
        self.last_refresh_time = 0.0  # monotonic seconds when last refreshed
        self.pending_files: set[str] = set()
        self.observer: Any | None = None
        self.event_handler = GitFileHandler(self)
        self.periodic_task: asyncio.Task | None = None
        self.is_running = False

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._start_file_watcher()
        # Initial PR cache population is handled by PRService.start(); avoid double refresh here.
        self.periodic_task = asyncio.create_task(self._periodic_refresh_loop())
        logger.info("Started GitHub refresh system for %s", self.worktree_path)

    def _start_file_watcher(self):
        if self.observer:
            return
        self.observer = Observer()
        git_dir = self.worktree_path / ".git"
        if git_dir.exists():
            self.observer.schedule(self.event_handler, str(git_dir), recursive=True)
            logger.debug("Watching .git directory: %s", git_dir)
        else:
            logger.warning("No .git directory found at %s", git_dir)
        self.observer.start()

    async def stop(self):
        self.is_running = False
        if self.observer:
            self.observer.stop()
            await asyncio.to_thread(self.observer.join)
            self.observer = None
        if self.pending_refresh_task:
            self.pending_refresh_task.cancel()
        if self.periodic_task:
            self.periodic_task.cancel()
        logger.info("Stopped GitHub refresh system for %s", self.worktree_path)

    def trigger_refresh(self, reason: str, file_path: str | None = None):
        if file_path:
            self.pending_files.add(file_path)
        logger.debug("GitHub refresh triggered: %s (file: %s)", reason, file_path)
        if self.pending_refresh_task:
            self.pending_refresh_task.cancel()
        self.pending_refresh_task = asyncio.create_task(self._debounced_refresh(reason))

    async def _debounced_refresh(self, reason: str):
        try:
            await asyncio.sleep(self.debounce_delay)
            if self.pending_refresh_task and not self.pending_refresh_task.done():
                await self._do_refresh(f"debounced: {reason}")
        except asyncio.CancelledError:
            logger.debug("Debounced refresh cancelled: %s", reason)

    async def _periodic_refresh_loop(self):
        while self.is_running:
            try:
                await asyncio.sleep(self.periodic_interval)
                current_time = time.monotonic()
                time_since_last = current_time - self.last_refresh_time
                if time_since_last >= self.periodic_interval * 0.8:
                    await self._do_refresh("periodic")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in periodic refresh")
                await asyncio.sleep(10)

    async def _do_refresh(self, reason: str):
        start_time = time.perf_counter()
        files_changed = list(self.pending_files)
        self.pending_files.clear()
        logger.info("Refreshing GitHub data: %s (files: %s)", reason, files_changed)
        # Do NOT fetch from network here; rely on API cache/background
        await self.refresh_callback(reason, files_changed)
        self.last_refresh_time = time.monotonic()
        refresh_time = (time.perf_counter() - start_time) * 1000
        logger.info("GitHub refresh completed in %.1fms", refresh_time)


class GitFileHandler(FileSystemEventHandler):
    """Handles file system events for git-related files in .git directory."""

    def __init__(self, refresh_system: DebouncedGitHubRefresh):
        self.refresh_system = refresh_system
        self.watched_patterns = {
            "refs/heads/",
            "refs/remotes/",
            "HEAD",
            "index",
            "COMMIT_EDITMSG",
            "FETCH_HEAD",
            "ORIG_HEAD",
        }

    def on_modified(self, event):
        if event.is_directory:
            return
        file_path = event.src_path
        if self._should_trigger_refresh(file_path):
            reason = f"git file modified: {Path(file_path).name}"
            self.refresh_system.trigger_refresh(reason, file_path)

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = event.src_path
        if self._should_trigger_refresh(file_path):
            reason = f"git file created: {Path(file_path).name}"
            self.refresh_system.trigger_refresh(reason, file_path)

    def _should_trigger_refresh(self, file_path: str) -> bool:
        path_str = str(file_path)
        for pattern in self.watched_patterns:
            if pattern in path_str:
                logger.debug("Git file change detected: %s (pattern: %s)", path_str, pattern)
                return True
        return False
