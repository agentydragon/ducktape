"""Shared data preparation for visual-review presentation."""

import urllib.parse
from collections import Counter
from typing import Any, Literal

from devinfra.pr_visuals.metadata import ClassificationCounts, ReviewAsset, ReviewTest


def _shard_ranges(shards: list[int | None]) -> str:
    numbers = sorted({shard for shard in shards if shard is not None})
    ranges: list[str] = []
    start = end = 0
    for number in numbers:
        if ranges and number == end + 1:
            end = number
            ranges[-1] = f"{start}-{end}"
        else:
            start = end = number
            ranges.append(str(number))
    if None in shards:
        ranges.append("unknown")
    return ", ".join(ranges)


def _ordered_assets(assets: list[ReviewAsset]) -> list[ReviewAsset]:
    """Put changed screenshots first and sort each class by review impact."""
    classification_order = {"unstable": 0, "modified": 1, "new": 2, "removed": 3, "unchanged": 4, None: 5}
    return sorted(
        assets,
        key=lambda asset: (
            classification_order[asset.classification],
            -(asset.changed_fraction or 0.0),
            asset.label.casefold(),
            asset.path,
        ),
    )


def _image_key(digest: str) -> str:
    return f"assets/{digest}.png"


def _asset_url(
    asset_base: str, asset: ReviewAsset, variant: Literal["candidate", "baseline", "diff"], *, shared_asset_base: str
) -> str:
    digest = {"candidate": asset.candidate_sha256, "baseline": asset.baseline_sha256, "diff": asset.diff_sha256}[
        variant
    ]
    if digest is not None:
        return f"{shared_asset_base}{_image_key(digest)}"
    prefix = {"candidate": "", "baseline": "baseline/", "diff": "diff/"}[variant]
    return f"{asset_base}{prefix}{urllib.parse.quote(asset.path, safe='/')}"


def _asset_page_data(asset: ReviewAsset, *, asset_base: str, page_url: str, shared_asset_base: str) -> dict[str, Any]:
    path = urllib.parse.quote(asset.path, safe="/")
    return {
        **asset.model_dump(exclude_none=True),
        "candidate_url": (
            f"{shared_asset_base}{_image_key(asset.variants[0].sha256)}"
            if asset.candidate_sha256 is None and asset.variants
            else _asset_url(asset_base, asset, "candidate", shared_asset_base=shared_asset_base)
        ),
        "baseline_url": _asset_url(asset_base, asset, "baseline", shared_asset_base=shared_asset_base),
        "diff_url": _asset_url(asset_base, asset, "diff", shared_asset_base=shared_asset_base),
        "anchor_url": f"{page_url}#{path}",
        "variants": [
            {**variant.model_dump(), "url": f"{shared_asset_base}{_image_key(variant.sha256)}"}
            for variant in asset.variants
        ],
    }


def _test_impact_key(test: ReviewTest) -> tuple[float, int, str]:
    changed = [asset for asset in test.assets if asset.classification in {"modified", "new", "removed", "unstable"}]
    largest_change = max(
        (asset.changed_fraction or 0.0 for asset in changed if asset.classification == "modified"), default=0.0
    )
    return (-largest_change, -len(changed), test.target_label)


def _totals(review_tests: list[ReviewTest]) -> ClassificationCounts:
    totals: Counter[str] = Counter()
    for test in review_tests:
        if test.summary is not None:
            totals.update(test.summary.model_dump())
    return ClassificationCounts(**totals)


def _buildbuddy_url(invocation: str) -> str:
    return f"https://app.buildbuddy.io/invocation/{urllib.parse.quote(invocation, safe='')}"
