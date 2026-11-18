"""Concurrent collectors share one tracefs mount and retain mount failures."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from agentplane.system_tests.vm import tracefs


def test_concurrent_collectors_mount_once(tmp_path: Path) -> None:
    def mount(*args: object, **kwargs: object) -> None:
        (tmp_path / "uprobe_events").touch()

    with (
        patch.object(tracefs.subprocess, "run", side_effect=mount) as mounting,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        list(workers.map(tracefs.ensure_mounted, [tmp_path, tmp_path]))
    assert mounting.call_count == 1


def test_mount_failure_reaches_collector(tmp_path: Path) -> None:
    with (
        patch.object(tracefs.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "mount")),
        pytest.raises(subprocess.CalledProcessError),
    ):
        tracefs.ensure_mounted(tmp_path)
