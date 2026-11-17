from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import pytest_bazel

from platform_sessions.provider import uncertainty_marker


def test_marker_creation_is_exclusive_private_and_visible_after_restart(tmp_path: Path):
    path = tmp_path / "operation-uncertain"
    marker = uncertainty_marker.DurableUncertaintyMarker(path)

    marker.mark_exclusive()

    assert marker.is_set()
    assert uncertainty_marker.DurableUncertaintyMarker(path).is_set()
    assert path.read_bytes() == b""
    assert (path.stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):
        marker.mark_exclusive()


def test_concurrent_marker_creation_admits_only_one_writer(tmp_path: Path):
    path = tmp_path / "operation-uncertain"
    barrier = threading.Barrier(8)

    def create_marker() -> bool:
        marker = uncertainty_marker.DurableUncertaintyMarker(path)
        barrier.wait()
        try:
            marker.mark_exclusive()
        except FileExistsError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: create_marker(), range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert uncertainty_marker.DurableUncertaintyMarker(path).is_set()


def test_ensure_set_syncs_an_existing_marker(tmp_path: Path, monkeypatch):
    path = tmp_path / "operation-uncertain"
    marker = uncertainty_marker.DurableUncertaintyMarker(path)
    marker.mark_exclusive()
    synced = []
    original_sync = uncertainty_marker.sync_directory

    def record_sync(directory: Path) -> None:
        synced.append(directory)
        original_sync(directory)

    monkeypatch.setattr(uncertainty_marker, "sync_directory", record_sync)

    assert not marker.ensure_set()
    assert synced == [tmp_path]


def test_missing_corrupt_and_dangling_markers_are_conservative(tmp_path: Path):
    path = tmp_path / "operation-uncertain"
    marker = uncertainty_marker.DurableUncertaintyMarker(path)

    assert not marker.is_set()
    path.write_text("unexpected marker contents", encoding="utf-8")
    assert marker.is_set()
    path.unlink()
    path.symlink_to(tmp_path / "missing-target")
    assert marker.is_set()


def test_clear_syncs_parent_after_removing_marker(tmp_path: Path, monkeypatch):
    path = tmp_path / "operation-uncertain"
    marker = uncertainty_marker.DurableUncertaintyMarker(path)
    marker.mark_exclusive()
    original_sync = uncertainty_marker.sync_directory
    synced = []

    def record_sync(directory: Path) -> None:
        assert not path.exists()
        synced.append(directory)
        original_sync(directory)

    monkeypatch.setattr(uncertainty_marker, "sync_directory", record_sync)

    assert marker.clear()
    assert synced == [tmp_path]
    assert not marker.is_set()
    assert not marker.clear()


if __name__ == "__main__":
    pytest_bazel.main()
