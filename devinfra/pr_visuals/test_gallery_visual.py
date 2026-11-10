"""Capture the generated PR visual gallery with mixed screenshot dimensions."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image, ImageDraw
from playwright.async_api import expect

from devinfra.pr_visuals.metadata import ReviewResult
from devinfra.pr_visuals.publisher import (
    DownloadedAsset,
    DownloadedVariant,
    DownloadedVisualTest,
    build_bundle,
    target_slug,
)
from util.testing.frontend_visual.capture import VisualHarness, VisualPage
from util.testing.frontend_visual.viewports import Viewport

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class _BaselineSource:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _sample_png(size: tuple[int, int], label: str | None = None) -> bytes:
    width, height = size
    image = Image.new("RGBA", size, "#e9edf3")
    # Scale up the default bitmap font so size labels remain legible in gallery thumbnails.
    text_scale = 4
    text_layer = Image.new("RGBA", (width // text_scale, height // text_scale))
    draw = ImageDraw.Draw(text_layer)
    draw.text((8, 8), f"{width} x {height}", fill="#263140")
    if label:
        draw.text((8, 24), label, fill="#52647d")
    image.alpha_composite(text_layer.resize(size, Image.Resampling.NEAREST))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _asset_key(path: str) -> DownloadedAsset:
    return DownloadedAsset(f"gallery/test_visual.py::{path}", None, "screenshot", path.removesuffix(".png"), [])


def _result(
    run: int,
    *,
    shard: int = 1,
    invocation: str = "12345678-1234-1234-1234-123456789abc",
    configuration: str = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
) -> ReviewResult:
    return ReviewResult(
        invocation=invocation, configuration=configuration, run=run, shard=shard, attempt=1, status="PASSED"
    )


def _add_test(
    root: Path,
    baseline_objects: dict[str, bytes],
    *,
    label: str,
    title: str,
    candidate: dict[str, bytes],
    baseline: dict[str, bytes],
    unstable: dict[str, bytes] | None = None,
) -> DownloadedVisualTest:
    slug = target_slug(label, repository="agentydragon/ducktape")
    source = root / slug
    source.mkdir(parents=True)
    assets: list[DownloadedAsset] = []
    for path, body in candidate.items():
        image = source / path
        image.write_bytes(body)
        base = _asset_key(path)
        results = [_result(1, shard=shard) for shard in (1, 2, 3)] if unstable and path in unstable else [_result(1)]
        variants = [DownloadedVariant(image, hashlib.sha256(body).hexdigest(), results)]
        if unstable and path in unstable:
            other = source / f"variant-{path}"
            other.write_bytes(unstable[path])
            variants.append(
                DownloadedVariant(
                    other,
                    hashlib.sha256(unstable[path]).hexdigest(),
                    [
                        _result(
                            2,
                            invocation="87654321-4321-4321-4321-cba987654321",
                            configuration="87654321fedcba0987654321fedcba0987654321fedcba0987654321fedcba09",
                        )
                    ],
                )
            )
        assets.append(DownloadedAsset(base.case_id, None, "screenshot", base.label, variants))

    baseline_assets = []
    for path, body in baseline.items():
        asset = _asset_key(path)
        digest = hashlib.sha256(body).hexdigest()
        baseline_objects[f"assets/{digest}.png"] = body
        baseline_assets.append(
            {
                "path": asset.path,
                "label": asset.label,
                "case_id": asset.case_id,
                "checkpoint": None,
                "role": "screenshot",
                "candidate_sha256": digest,
            }
        )
    metadata = {
        "assets": baseline_assets,
        "baseline_eligible": True,
        "comparison_profile": "bazel-configuration-exact-v1",
        "configuration": "gallery",
    }
    prefix = f"reviews-test-outputs/{BASE_SHA}/tests/{slug}/"
    baseline_objects[f"{prefix}metadata.json"] = json.dumps(metadata).encode()
    return DownloadedVisualTest(label, slug, title, assets, "gallery", [])


def _gallery_bundle(tmp_path: Path) -> tuple[Path, str]:
    baseline_objects: dict[str, bytes] = {}
    low_label = "//gallery:low_impact"
    high_label = "//gallery:high_impact"
    low = _add_test(
        tmp_path / "source",
        baseline_objects,
        label=low_label,
        title="Lower impact sample",
        baseline={"list.png": _sample_png((800, 500), "before")},
        candidate={"list.png": _sample_png((800, 500), "after")},
    )
    high = _add_test(
        tmp_path / "source",
        baseline_objects,
        label=high_label,
        title="Mixed dimensions and statuses",
        baseline={
            "dashboard.png": _sample_png((960, 540), "before"),
            "portrait.png": _sample_png((360, 760), "before"),
            "stable.png": _sample_png((640, 400)),
            "removed.png": _sample_png((1000, 600), "removed"),
        },
        candidate={
            "dashboard.png": _sample_png((960, 540), "after"),
            "portrait.png": _sample_png((760, 360), "after"),
            "stable.png": _sample_png((640, 400)),
            "new-portrait.png": _sample_png((320, 900), "new"),
        },
        unstable={"new-portrait.png": _sample_png((320, 900), "other run")},
    )
    bundle = build_bundle(
        [low, high],
        tmp_path / "site",
        commit_sha=HEAD_SHA,
        repository="agentydragon/ducktape",
        base_sha=BASE_SHA,
        baseline_source=_BaselineSource(baseline_objects),
    )
    return bundle, high.slug


async def _assert_images_decode(view: VisualPage) -> None:
    page = view.page
    await page.locator("img").evaluate_all("images => images.forEach(image => { image.loading = 'eager'; })")
    decoded = await page.evaluate(
        """async () => {
          await Promise.all(Array.from(document.images, image => image.decode()));
          return Array.from(document.images, image => image.naturalWidth > 0);
        }"""
    )
    assert all(decoded), "the generated gallery contains an image that failed to load"


async def _assert_images_preserve_aspect_ratio(view: VisualPage) -> None:
    measurements = await view.page.locator(".sample-frame img").evaluate_all(
        """images => images
          .filter(image => image.getClientRects().length)
          .map(image => {
            const imageRect = image.getBoundingClientRect();
            const frameRect = image.closest('.sample-frame').getBoundingClientRect();
            const style = getComputedStyle(image);
            return {
              objectFit: style.objectFit,
              fits: Math.abs(imageRect.width - frameRect.width) < 1 && Math.abs(imageRect.height - frameRect.height) < 1,
            };
          })"""
    )
    assert measurements
    assert all(item["objectFit"] == "contain" and item["fits"] for item in measurements), measurements


async def _assert_sample_frame_ratios(view: VisualPage) -> None:
    page = view.page
    ratios = await page.locator(".sample-frame").evaluate_all(
        """frames => frames
          .filter(frame => frame.getClientRects().length)
          .map(frame => {
            const { width, height } = frame.getBoundingClientRect();
            return width / height;
          })"""
    )
    assert ratios
    assert all(abs(ratio - 1.6) < 0.03 for ratio in ratios), ratios


async def test_generated_gallery_layout(visual: VisualHarness, tmp_path: Path) -> None:
    bundle, high_slug = _gallery_bundle(tmp_path)
    index_url = (bundle / "index.html").absolute().as_uri()
    detail_url = (bundle / "tests" / high_slug / "index.html").absolute().as_uri()

    async with visual.open(viewport=Viewport(width=1440, height=1000), trace_checkpoint="desktop") as view:
        await view.page.goto(index_url, wait_until="load")
        await expect(view.page.locator("h2").first).to_contain_text("Mixed dimensions and statuses")
        await expect(view.page.locator(".assets .sample-frame")).to_have_count(2)
        sort = view.page.get_by_label("Sort tests")
        await sort.select_option("name")
        await expect(view.page.locator("h2").first).to_contain_text("Lower impact sample")
        await sort.select_option("changed")
        await expect(view.page.locator("h2").first).to_contain_text("Mixed dimensions and statuses")
        await sort.select_option("impact")
        await expect(view.page.locator("h2").first).to_contain_text("Mixed dimensions and statuses")
        await _assert_images_decode(view)
        await _assert_images_preserve_aspect_ratio(view)
        await _assert_sample_frame_ratios(view)
        await view.capture("gallery-index", full_page=True)

        await view.page.goto(detail_url, wait_until="load")
        await expect(view.page.get_by_role("heading", name="Mixed dimensions and statuses")).to_be_visible()
        landscape_pair = view.page.locator(f'figure[id="{_asset_key("dashboard.png").path}"] .pair')
        slider = view.page.locator(f'figure[id="{_asset_key("dashboard.png").path}"] img-comparison-slider')
        await expect(slider).to_be_visible()
        await expect(slider.locator('img[slot="first"]')).to_have_attribute("alt", "dashboard before")
        await expect(slider.locator('img[slot="second"]')).to_have_attribute("alt", "dashboard after")
        await expect(landscape_pair).to_be_hidden()
        await view.page.get_by_text("View before and after separately").click()
        await expect(landscape_pair.locator(":scope > div").nth(0)).to_contain_text("Before")
        await expect(landscape_pair.locator(":scope > div").nth(1)).to_contain_text("After")
        dimension_change = view.page.locator(f'figure[id="{_asset_key("portrait.png").path}"]')
        await expect(dimension_change.get_by_role("note")).to_contain_text("dimensions changed")
        await expect(dimension_change.locator("img-comparison-slider")).to_have_count(0)
        await expect(dimension_change.locator(".sample-frame img")).to_have_count(2)
        unstable_asset = view.page.locator(f'figure[id="{_asset_key("new-portrait.png").path}"]')
        await expect(unstable_asset).to_contain_text("unstable across runs")
        await expect(unstable_asset).to_contain_text("run 1 · shards 1-3 · attempt 1 · config 12345678")
        await expect(unstable_asset).to_contain_text("run 2 · shard 1 · attempt 1 · config 87654321")
        await expect(unstable_asset.get_by_role("link", name="BuildBuddy 12345678")).to_have_attribute(
            "href", "https://app.buildbuddy.io/invocation/12345678-1234-1234-1234-123456789abc"
        )
        await expect(unstable_asset.get_by_role("link", name="BuildBuddy 87654321")).to_have_attribute(
            "href", "https://app.buildbuddy.io/invocation/87654321-4321-4321-4321-cba987654321"
        )
        await _assert_images_decode(view)
        await _assert_images_preserve_aspect_ratio(view)
        await _assert_sample_frame_ratios(view)
        await view.capture("gallery-detail-desktop", full_page=True)

    async with visual.open(viewport=Viewport(width=412, height=915), trace_checkpoint="mobile") as view:
        await view.page.goto(detail_url, wait_until="load")
        columns = await view.page.locator(f'figure[id="{_asset_key("dashboard.png").path}"] .pair').evaluate(
            "pair => getComputedStyle(pair).gridTemplateColumns.split(' ').length"
        )
        assert columns == 1
        await _assert_images_decode(view)
        await _assert_images_preserve_aspect_ratio(view)
        await view.capture("gallery-detail-mobile", full_page=True)


if __name__ == "__main__":
    pytest_bazel.main()
