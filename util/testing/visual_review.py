"""Write the versioned visual-review manifest consumed by trusted CI."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path

from util.testing.undeclared_outputs import undeclared_outputs_dir
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


def retain_review_asset(
    png_path: Path, *, title: str, label: str, name: str | None = None, output_dir: Path | None = None
) -> Path:
    """Copy `png_path` into undeclared test outputs and upsert the manifest.

    One call per rendered case: the manifest accumulates assets across calls
    within a test execution, so parametrized tests never need the full case
    list upfront. The trusted publisher (devinfra/pr_visuals/publisher.py) picks
    manifest + assets up from passing CI runs.

    `name` overrides the published asset basename (defaults to the source
    file's); `output_dir` overrides the undeclared-outputs destination (tests
    of this helper only).
    """
    out_dir = output_dir if output_dir is not None else undeclared_outputs_dir()
    asset_name = name or png_path.name
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png_path, out_dir / asset_name)
    upsert_review_asset(out_dir, title=title, asset=VisualReviewAsset(path=asset_name, label=label))
    return out_dir / asset_name
