"""Write the versioned visual-review manifest consumed by trusted CI."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA = "ducktape.visual-review.v1"
MANIFEST_NAME = "visual-review.json"


@dataclass(frozen=True)
class VisualReviewAsset:
    path: str
    label: str


def write_visual_review_manifest(output_dir: Path, *, title: str, assets: Iterable[VisualReviewAsset]) -> Path:
    asset_list = list(assets)
    if not title.strip():
        raise ValueError("visual-review title must not be empty")
    if not asset_list:
        raise ValueError("visual review must contain at least one asset")
    paths = [asset.path for asset in asset_list]
    if any(Path(path).name != path or not path.endswith(".png") for path in paths):
        raise ValueError("visual-review assets must be safe PNG basenames")
    if len(paths) != len(set(paths)):
        raise ValueError("visual-review asset paths must be unique")

    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / MANIFEST_NAME
    destination.write_text(
        json.dumps({"schema": SCHEMA, "title": title, "assets": [asdict(asset) for asset in asset_list]}, indent=2)
        + "\n"
    )
    return destination
