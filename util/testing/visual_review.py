"""Write the versioned visual-review manifest consumed by trusted CI."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from util.visual_review import MANIFEST_NAME, VisualReviewAsset, VisualReviewManifest


def write_visual_review_manifest(output_dir: Path, *, title: str, assets: Iterable[VisualReviewAsset]) -> Path:
    manifest = VisualReviewManifest(title=title, assets=list(assets))

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / MANIFEST_NAME
    # Literal UTF-8 rather than `\uXXXX` escapes, so a label such as "hot · dark" stays readable.
    destination.write_text(
        json.dumps(manifest.model_dump(by_alias=True), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return destination


def upsert_review_asset(output_dir: Path, *, title: str, asset: VisualReviewAsset) -> Path:
    """Add `asset` to the manifest in `output_dir`, creating it if absent.

    Lets a runner publish renders one at a time without knowing the full list upfront. An asset
    whose path is already listed keeps its first label.
    """
    manifest_path = output_dir / MANIFEST_NAME
    assets = (
        list(VisualReviewManifest.model_validate_json(manifest_path.read_bytes()).assets)
        if manifest_path.exists()
        else []
    )
    if all(existing.path != asset.path for existing in assets):
        assets.append(asset)
    return write_visual_review_manifest(output_dir, title=title, assets=assets)


def publish_review_png(png: bytes, *, output_dir: Path, title: str, name: str, label: str) -> Path:
    """Publish one capture; output names are unique within a test target execution."""
    asset = VisualReviewAsset(path=name, label=label)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / asset.path
    # Exclusive creation catches two tests claiming the same output before either can overwrite it.
    with destination.open("xb") as stream:
        stream.write(png)
    upsert_review_asset(output_dir, title=title, asset=asset)
    return destination
