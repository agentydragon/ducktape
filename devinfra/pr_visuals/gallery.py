"""Gallery template setup and view data."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from devinfra.pr_visuals.metadata import ReviewTest
from devinfra.pr_visuals.render_common import (
    asset_page_data,
    buildbuddy_url,
    ordered_assets,
    provenance_labels_for_test,
    shard_ranges,
    test_impact_key,
)


def templates() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )
    environment.filters["shard_ranges"] = shard_ranges
    environment.filters["buildbuddy_url"] = buildbuddy_url
    return environment


def review_test_page_data(
    test: ReviewTest, *, asset_base: str, page_url: str, shared_asset_base: str
) -> dict[str, Any]:
    page = test.model_dump(exclude_none=True)
    invocation_labels, configuration_labels = provenance_labels_for_test(test)
    for issue in [*page["issues"], *page["notices"]]:
        if issue.get("source") is not None:
            source = issue["source"]
            source["invocation_label"] = invocation_labels[source["invocation"]]
            if source.get("configuration") is not None:
                source["configuration_label"] = configuration_labels[source["configuration"]]
    page["assets"] = [
        asset_page_data(
            asset,
            asset_base=asset_base,
            page_url=page_url,
            shared_asset_base=shared_asset_base,
            invocation_labels=invocation_labels,
            configuration_labels=configuration_labels,
        )
        for asset in ordered_assets(test.assets)
    ]
    impact_key = test_impact_key(test)
    page["sort_changed_count"] = -impact_key[1]
    page["sort_largest_change"] = -impact_key[0]
    if test.preview is not None:
        page["preview"] = asset_page_data(
            test.preview,
            asset_base=asset_base,
            page_url=page_url,
            shared_asset_base=shared_asset_base,
            invocation_labels=invocation_labels,
            configuration_labels=configuration_labels,
        )
    return page
