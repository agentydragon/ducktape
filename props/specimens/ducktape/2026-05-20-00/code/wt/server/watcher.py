"""Filesystem watcher for worktree directory changes."""

from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from wt.server.stores import DaemonStore


def _to_path(src_path: bytes | str) -> Path:
    """Convert watchdog src_path to Path, handling bytes case."""
    if isinstance(src_path, bytes):
        return Path(src_path.decode())
    return Path(src_path)


class WorktreeWatcher(FileSystemEventHandler):
    """Watches worktrees directory and updates store on changes."""

    def __init__(self, store: DaemonStore, worktrees_dir: Path):
        self.store = store
        self.worktrees_dir = worktrees_dir

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        if not event.is_directory:
            return
        path = _to_path(event.src_path)
        # Only react to direct children of worktrees_dir
        if path.parent == self.worktrees_dir:
            self.store.add_worktree_path(path)

    def on_deleted(self, event: DirDeletedEvent | FileDeletedEvent) -> None:
        if not event.is_directory:
            return
        path = _to_path(event.src_path)
        if path.parent == self.worktrees_dir:
            self.store.remove_worktree_path(path)

    def on_moved(self, event: DirMovedEvent | FileMovedEvent) -> None:
        if not event.is_directory:
            return
        src_path = _to_path(event.src_path)
        dest_path = _to_path(event.dest_path)
        # Handle rename within worktrees dir
        if src_path.parent == self.worktrees_dir:
            self.store.remove_worktree_path(src_path)
        if dest_path.parent == self.worktrees_dir:
            self.store.add_worktree_path(dest_path)


def start_watcher(store: DaemonStore, worktrees_dir: Path) -> BaseObserver:
    """Start watching the worktrees directory. Returns observer (caller must stop on shutdown)."""
    observer = Observer()
    handler = WorktreeWatcher(store, worktrees_dir)
    observer.schedule(handler, str(worktrees_dir), recursive=False)
    observer.start()
    return observer
