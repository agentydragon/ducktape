"""Shared data preparation for visual-review presentation."""

import urllib.parse
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from devinfra.pr_visuals.metadata import ClassificationCounts, ReviewAsset, ReviewResult, ReviewTest


class ProvenanceSource(Protocol):
    invocation: str
    configuration: str | None


def shard_ranges(shards: list[int | None]) -> str:
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


def ordered_assets(assets: list[ReviewAsset]) -> list[ReviewAsset]:
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


def shared_image_key(digest: str) -> str:
    return f"assets/{digest}.png"


def _asset_url(
    asset_base: str, asset: ReviewAsset, variant: Literal["candidate", "baseline", "diff"], *, shared_asset_base: str
) -> str:
    digest = {"candidate": asset.candidate_sha256, "baseline": asset.baseline_sha256, "diff": asset.diff_sha256}[
        variant
    ]
    if digest is not None:
        return f"{shared_asset_base}{shared_image_key(digest)}"
    prefix = {"candidate": "", "baseline": "baseline/", "diff": "diff/"}[variant]
    return f"{asset_base}{prefix}{urllib.parse.quote(asset.path, safe='/')}"


def asset_page_data(
    asset: ReviewAsset,
    *,
    asset_base: str,
    page_url: str,
    shared_asset_base: str,
    invocation_labels: Mapping[str, str],
    configuration_labels: Mapping[str, str],
) -> dict[str, Any]:
    path = urllib.parse.quote(asset.path, safe="/")
    return {
        **asset.model_dump(exclude_none=True),
        "candidate_url": (
            f"{shared_asset_base}{shared_image_key(asset.variants[0].sha256)}"
            if asset.candidate_sha256 is None and asset.variants
            else _asset_url(asset_base, asset, "candidate", shared_asset_base=shared_asset_base)
        ),
        "baseline_url": _asset_url(asset_base, asset, "baseline", shared_asset_base=shared_asset_base),
        "diff_url": _asset_url(asset_base, asset, "diff", shared_asset_base=shared_asset_base),
        "anchor_url": f"{page_url}#{path}",
        "variants": [
            {
                **variant.model_dump(),
                "url": f"{shared_asset_base}{shared_image_key(variant.sha256)}",
                "provenance": variant_provenance(
                    variant.results, invocation_labels=invocation_labels, configuration_labels=configuration_labels
                ),
            }
            for variant in asset.variants
        ],
    }


def test_impact_key(test: ReviewTest) -> tuple[float, int, str]:
    changed = [asset for asset in test.assets if asset.classification in {"modified", "new", "removed", "unstable"}]
    largest_change = max(
        (asset.changed_fraction or 0.0 for asset in changed if asset.classification == "modified"), default=0.0
    )
    return (-largest_change, -len(changed), test.target_label)


def classification_totals(review_tests: list[ReviewTest]) -> ClassificationCounts:
    totals: Counter[str] = Counter()
    for test in review_tests:
        if test.summary is not None:
            totals.update(test.summary.model_dump())
    return ClassificationCounts(**totals)


def buildbuddy_url(invocation: str) -> str:
    return f"https://app.buildbuddy.io/invocation/{urllib.parse.quote(invocation, safe='')}"


def variant_provenance(
    results: list[ReviewResult], *, invocation_labels: Mapping[str, str], configuration_labels: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Group image provenance by invocation, configuration, run, and attempt."""
    grouped: dict[tuple[str, str | None, int | None, int | None], set[int | None]] = {}
    for result in results:
        key = (result.invocation, result.configuration, result.run, result.attempt)
        grouped.setdefault(key, set()).add(result.shard)

    provenance = []
    for (invocation, configuration, run, attempt), shards in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2] or 0, item[0][3] or 0)
    ):
        provenance.append(
            {
                "invocation": invocation,
                "invocation_label": invocation_labels[invocation],
                "invocation_url": buildbuddy_url(invocation),
                "configuration_label": configuration_labels.get(configuration) if configuration is not None else None,
                "run": run,
                "shards": sorted(shards, key=lambda shard: (shard is None, shard or 0)),
                "attempt": attempt,
            }
        )
    return provenance


def provenance_labels_for_test(test: ReviewTest) -> tuple[dict[str, str], dict[str, str]]:
    """Create collision-safe labels across every provenance source belonging to a target."""
    sources: Sequence[ProvenanceSource] = [
        *(issue.source for issue in [*test.issues, *test.notices] if issue.source is not None),
        *(
            result
            for asset in [*test.assets, *([test.preview] if test.preview is not None else [])]
            for variant in asset.variants
            for result in variant.results
        ),
    ]
    invocation_labels = unique_prefixes({source.invocation for source in sources})
    configuration_labels = unique_prefixes(
        {source.configuration for source in sources if source.configuration is not None}
    )
    return invocation_labels, configuration_labels


def unique_prefixes(values: set[str], *, minimum_length: int = 8) -> dict[str, str]:
    lengths = {value: min(minimum_length, len(value)) for value in values}
    while True:
        colliding: dict[str, list[str]] = {}
        for value, length in lengths.items():
            colliding.setdefault(value[:length], []).append(value)
        duplicate_groups = [group for group in colliding.values() if len(group) > 1]
        if not duplicate_groups:
            return {value: value[:length] for value, length in lengths.items()}
        for group in duplicate_groups:
            for value in group:
                lengths[value] = min(lengths[value] + 1, len(value))
