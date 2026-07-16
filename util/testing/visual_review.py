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
    destination.write_text(json.dumps(manifest.model_dump(by_alias=True), indent=2) + "\n")
    return destination


def retain_review_asset(
    png_path: Path, *, title: str, label: str, name: str | None = None, output_dir: Path | None = None
) -> Path:
    """Copy `png_path` into undeclared test outputs and upsert the manifest.

    One call per rendered case: the manifest accumulates assets across calls
    within a test execution, so parametrized tests never need the full case
    list upfront. The trusted publisher (devinfra/ci/pr_visuals.py) picks
    manifest + assets up from passing CI runs.

    `name` overrides the published asset basename (defaults to the source
    file's); `output_dir` overrides the undeclared-outputs destination (tests
    of this helper only).
    """
    out_dir = output_dir if output_dir is not None else undeclared_outputs_dir()
    asset_name = name or png_path.name
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png_path, out_dir / asset_name)

    manifest_path = out_dir / MANIFEST_NAME
    assets = (
        list(VisualReviewManifest.model_validate_json(manifest_path.read_text()).assets)
        if manifest_path.exists()
        else []
    )
    if all(asset.path != asset_name for asset in assets):
        assets.append(VisualReviewAsset(path=asset_name, label=label))
    write_visual_review_manifest(out_dir, title=title, assets=assets)
    return out_dir / asset_name
