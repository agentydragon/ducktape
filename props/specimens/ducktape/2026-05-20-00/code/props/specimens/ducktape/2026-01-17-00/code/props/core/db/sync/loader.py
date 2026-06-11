"""Load snapshot manifests for sync operation."""

from __future__ import annotations

from pathlib import Path

import yaml

from ...models.snapshot import SnapshotDoc, SnapshotSlug


def _derive_slug_from_path(snapshot_dir: Path, specimens_dir: Path) -> str | None:
    """Derive slug from directory path relative to specimens root."""
    try:
        rel = snapshot_dir.relative_to(specimens_dir)
        parts = rel.parts
        if len(parts) != 2:  # Must be repo/version
            return None
        return f"{parts[0]}/{parts[1]}"
    except ValueError:
        return None


def discover_snapshots(specimens_dir: Path) -> dict[SnapshotSlug, SnapshotDoc]:
    """Discover snapshots by scanning for manifest.yaml files."""
    specimens_dir = specimens_dir.resolve()
    snapshots: dict[SnapshotSlug, SnapshotDoc] = {}

    for manifest_path in specimens_dir.rglob("manifest.yaml"):
        slug_str = _derive_slug_from_path(manifest_path.parent, specimens_dir)
        if slug_str is None:
            continue

        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        doc = SnapshotDoc.model_validate(raw)
        snapshots[SnapshotSlug(slug_str)] = doc

    return snapshots
