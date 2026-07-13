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
    destination.write_text(json.dumps(manifest.model_dump(by_alias=True), indent=2) + "\n")
    return destination
