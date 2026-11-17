"""Shared tracefs mount ownership for concurrent VM diagnostic collectors."""

import subprocess
import threading
from pathlib import Path

_mount_lock = threading.Lock()


def ensure_mounted(root: Path) -> None:
    # Both observers start at domain discovery. Independent locks can stack
    # mounts and hide one collector's instance from its cleanup path.
    with _mount_lock:
        if not (root / "uprobe_events").exists():
            subprocess.run(["mount", "-t", "tracefs", "tracefs", str(root)], check=True, capture_output=True, timeout=2)
