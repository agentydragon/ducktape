"""Capture the generated PR visual gallery with mixed screenshot dimensions."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import pytest_bazel
from PIL import Image, ImageDraw
from playwright.async_api import expect

from devinfra.pr_visuals.publisher import DownloadedVisualTest, build_bundle, target_slug
from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness, VisualPage
from util.visual_review import VisualReviewAsset, VisualReviewManifest

# gazelle:include_dep //util/testing:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"


class _BaselineSource:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def fetch(self, key: str) -> bytes | None:
        return self.objects.get(key)


def _sample_png(size: tuple[int, int], *, accent: str, title: str) -> bytes:
    width, height = size
    image = Image.new("RGB", size, "#f1f3f6")
    draw = ImageDraw.Draw(image)
    margin_x, margin_y = round(width * 0.06), round(height * 0.08)
    draw.rounded_rectangle(
        (margin_x, margin_y, width - margin_x, height - margin_y),
        radius=max(4, round(min(width, height) * 0.035)),
        fill="white",
        outline="#cbd0d8",
        width=max(1, round(min(width, height) * 0.004)),
    )
    header_bottom = margin_y + round((height - 2 * margin_y) * 0.2)
    draw.rectangle((margin_x, margin_y, width - margin_x, header_bottom), fill=accent)
    draw.text((margin_x + 12, margin_y + 10), title, fill="white")
    card_top = header_bottom + round(height * 0.07)
    draw.rounded_rectangle(
        (margin_x + 12, card_top, width - margin_x - 12, height - margin_y - 12), radius=8, fill="#e7eaf0"
    )
    draw.rectangle((margin_x + 24, card_top + 18, width - margin_x - 24, card_top + 28), fill="#8b93a1")
    draw.rectangle((margin_x + 24, card_top + 42, width - margin_x - 58, card_top + 50), fill=accent)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _add_test(
    root: Path,
    baseline_objects: dict[str, bytes],
    *,
    label: str,
    title: str,
    candidate: dict[str, bytes],
    baseline: dict[str, bytes],
) -> DownloadedVisualTest:
    slug = target_slug(label)
    source = root / slug
    source.mkdir(parents=True)
    for path, body in candidate.items():
        (source / path).write_bytes(body)

    metadata = {
        "target_label": label,
        "slug": slug,
        "title": title,
        "assets": [{"path": path, "label": path.removesuffix(".png")} for path in baseline],
    }
    prefix = f"commits/{BASE_SHA}/tests/{slug}/"
    baseline_objects[f"{prefix}metadata.json"] = json.dumps(metadata).encode()
    baseline_objects.update({f"{prefix}{path}": body for path, body in baseline.items()})

    manifest = VisualReviewManifest(
        title=title, assets=[VisualReviewAsset(path=path, label=path.removesuffix(".png")) for path in candidate]
    )
    return DownloadedVisualTest(label, slug, manifest, source)


def _gallery_bundle(tmp_path: Path) -> tuple[Path, str]:
    baseline_objects: dict[str, bytes] = {}
    low_label = "//gallery:low_impact"
    high_label = "//gallery:high_impact"
    low = _add_test(
        tmp_path / "source",
        baseline_objects,
        label=low_label,
        title="Lower impact sample",
        baseline={"list.png": _sample_png((800, 500), accent="#2878a8", title="List before")},
        candidate={"list.png": _sample_png((800, 500), accent="#338e67", title="List after")},
    )
    high = _add_test(
        tmp_path / "source",
        baseline_objects,
        label=high_label,
        title="Mixed dimensions and statuses",
        baseline={
            "dashboard.png": _sample_png((960, 540), accent="#2878a8", title="Dashboard before"),
            "portrait.png": _sample_png((360, 760), accent="#7656a8", title="Portrait before"),
            "stable.png": _sample_png((640, 400), accent="#52647d", title="Unchanged"),
            "removed.png": _sample_png((1000, 600), accent="#a06542", title="Removed sample"),
        },
        candidate={
            "dashboard.png": _sample_png((960, 540), accent="#2e9c55", title="Dashboard after"),
            "portrait.png": _sample_png((760, 360), accent="#c24d45", title="Landscape after"),
            "stable.png": _sample_png((640, 400), accent="#52647d", title="Unchanged"),
            "new-portrait.png": _sample_png((320, 900), accent="#a34f8a", title="New portrait"),
        },
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

    async with visual.open(viewport=Viewport(width=1440, height=1000)) as view:
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
        landscape_pair = view.page.locator('figure[id="dashboard.png"] .pair')
        await expect(landscape_pair.locator(":scope > div").nth(0)).to_contain_text("Before")
        await expect(landscape_pair.locator(":scope > div").nth(1)).to_contain_text("After")
        dimension_change = view.page.locator('figure[id="portrait.png"]')
        await expect(dimension_change.get_by_role("note")).to_contain_text("dimensions changed")
        await expect(dimension_change.locator(".sample-frame img")).to_have_count(2)
        await _assert_images_decode(view)
        await _assert_images_preserve_aspect_ratio(view)
        await _assert_sample_frame_ratios(view)
        await view.capture("gallery-detail-desktop", full_page=True)

    async with visual.open(viewport=Viewport(width=412, height=915)) as view:
        await view.page.goto(detail_url, wait_until="load")
        columns = await view.page.locator('figure[id="dashboard.png"] .pair').evaluate(
            "pair => getComputedStyle(pair).gridTemplateColumns.split(' ').length"
        )
        assert columns == 1
        await _assert_images_decode(view)
        await _assert_images_preserve_aspect_ratio(view)
        await view.capture("gallery-detail-mobile", full_page=True)


if __name__ == "__main__":
    pytest_bazel.main()
