"""Gallery template setup and view data."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from devinfra.pr_visuals.metadata import ReviewTest
from devinfra.pr_visuals.render_common import (
    _asset_page_data,
    _buildbuddy_url,
    _ordered_assets,
    _shard_ranges,
    _test_impact_key,
)


def _templates() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parent),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )
    environment.filters["shard_ranges"] = _shard_ranges
    environment.filters["buildbuddy_url"] = _buildbuddy_url
    return environment


def _review_test_page_data(
    test: ReviewTest, *, asset_base: str, page_url: str, shared_asset_base: str
) -> dict[str, Any]:
    page = test.model_dump(exclude_none=True)
    page["assets"] = [
        _asset_page_data(asset, asset_base=asset_base, page_url=page_url, shared_asset_base=shared_asset_base)
        for asset in _ordered_assets(test.assets)
    ]
    impact_key = _test_impact_key(test)
    page["sort_changed_count"] = -impact_key[1]
    page["sort_largest_change"] = -impact_key[0]
    if test.preview is not None:
        page["preview"] = _asset_page_data(
            test.preview, asset_base=asset_base, page_url=page_url, shared_asset_base=shared_asset_base
        )
    return page
