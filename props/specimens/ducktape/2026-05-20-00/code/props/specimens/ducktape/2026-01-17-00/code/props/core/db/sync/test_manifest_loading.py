"""Tests for per-snapshot manifest.yaml loading.

Uses git-tracked test fixtures at tests/props/fixtures/specimens/ to verify
manifest loading works correctly. Only creates temp directories for edge cases
that can't be represented in git fixtures (invalid YAML, missing manifests).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from props.core.db.sync.loader import discover_snapshots
from props.core.ids import SnapshotSlug


def test_load_git_fixtures(test_specimens_base: Path) -> None:
    """Test loading snapshots from git-tracked test fixtures.

    Verifies discover_snapshots finds all 4 test snapshots with correct slugs and splits.
    """
    snapshots = discover_snapshots(test_specimens_base)

    # Should find all 4 test fixtures
    assert len(snapshots) == 4

    # Verify each expected snapshot exists with correct split
    expected = {
        "test-fixtures/train1": "train",
        "test-fixtures/valid1": "valid",
        "test-fixtures/valid2": "valid",
        "test-fixtures/test1": "test",
    }
    for slug, expected_split in expected.items():
        assert SnapshotSlug(slug) in snapshots, f"Missing snapshot: {slug}"
        assert snapshots[SnapshotSlug(slug)].split == expected_split


def test_ignores_nested_manifests(tmp_path: Path) -> None:
    """Test that manifest.yaml at wrong depth is ignored."""
    # Create valid snapshot
    valid_dir = tmp_path / "repo" / "version"
    valid_dir.mkdir(parents=True)
    (valid_dir / "manifest.yaml").write_text(
        yaml.safe_dump({"source": {"vcs": "local", "root": "code"}, "split": "train"})
    )

    # Create nested manifest (too deep - should be ignored)
    nested_dir = tmp_path / "repo" / "version" / "subdir"
    nested_dir.mkdir(parents=True)
    (nested_dir / "manifest.yaml").write_text(
        yaml.safe_dump({"source": {"vcs": "local", "root": "code"}, "split": "valid"})
    )

    # Create shallow manifest (too shallow - should be ignored)
    (tmp_path / "manifest.yaml").write_text(
        yaml.safe_dump({"source": {"vcs": "local", "root": "code"}, "split": "test"})
    )

    snapshots = discover_snapshots(tmp_path)

    # Only the valid depth manifest should be loaded
    assert len(snapshots) == 1
    assert SnapshotSlug("repo/version") in snapshots


def test_no_manifests_returns_empty(tmp_path: Path) -> None:
    """Test that missing manifests returns empty dict."""
    snapshots = discover_snapshots(tmp_path)
    assert snapshots == {}


def test_invalid_manifest_raises(tmp_path: Path) -> None:
    """Test that invalid YAML in manifest raises Pydantic ValidationError."""
    snapshot_dir = tmp_path / "repo" / "version"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "manifest.yaml").write_text("not a mapping")

    # Pydantic raises ValidationError for non-dict input
    with pytest.raises(ValidationError):
        discover_snapshots(tmp_path)
