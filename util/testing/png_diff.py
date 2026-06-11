"""Pixel-tolerant PNG golden comparison with debug-artifact dumping.

Designed for visual regression tests that screenshot real GUIs (gnome-shell,
FreeCAD, etc.) where sub-pixel font rasterization noise can drift across runs
even when nothing meaningful changed.

`assert_png_matches_golden` writes `<name>.{actual,expected,diff}.png` to a
caller-supplied output dir on failure (typically the bazel undeclared
outputs dir, so the artifacts ride out to BuildBuddy).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageChops


def _per_pixel_max_channel_diff(actual: Image.Image, expected: Image.Image) -> Image.Image:
    """Return an L-mode image of the per-pixel max RGB channel difference.

    `ImageChops.difference` returns the absolute per-channel diff; collapsing
    via `convert("L")` would apply ITU-R luminance weights and under-count
    pure-red or pure-blue diffs (a 50-unit red change drops to 50*0.299=15
    luminance, slipping under typical thresholds). Take channel-wise max
    instead, which is the per-pixel L-infinity norm.
    """
    diff = ImageChops.difference(actual.convert("RGB"), expected.convert("RGB"))
    r, g, b = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def png_diff_fraction(actual_path: Path, expected_path: Path, intensity_threshold: int) -> tuple[float, Image.Image]:
    """Diff two PNGs and return (fraction_differing, overlay_image_with_red_diff_mask).

    Pixels whose per-channel max difference is below ``intensity_threshold``
    (0-255) are considered equal. The returned overlay is the actual image
    with mismatched pixels painted bright red — useful for eyeball debugging.
    """
    actual = Image.open(actual_path).convert("RGBA")
    expected = Image.open(expected_path).convert("RGBA")
    if actual.size != expected.size:
        raise AssertionError(f"PNG size mismatch: actual={actual.size} expected={expected.size}")

    max_channel_diff = _per_pixel_max_channel_diff(actual, expected)
    mask = max_channel_diff.point(lambda v: 255 if v >= intensity_threshold else 0)
    overlay = actual.copy()
    red = Image.new("RGBA", actual.size, (255, 0, 0, 255))
    overlay.paste(red, mask=mask)

    # mask histogram is a 256-bucket count; bucket 255 is the count of
    # mismatched pixels. Avoids materializing the full pixel buffer.
    differing = mask.histogram()[255]
    fraction = differing / (actual.size[0] * actual.size[1])
    return fraction, overlay


def assert_png_matches_golden(
    actual_path: Path,
    expected_path: Path,
    *,
    name: str,
    out_dir: Path,
    tolerance: float = 0.02,
    intensity_threshold: int = 16,
) -> None:
    """Assert that ``actual_path`` matches ``expected_path`` within tolerance.

    On failure, copies ``<name>.{actual,expected,diff}.png`` into ``out_dir``
    (typically bazel undeclared outputs) and raises ``AssertionError`` with a
    pointer to the artifacts. On success, writes nothing — successful runs
    don't pollute undeclared outputs / BuildBuddy artifacts.

    ``tolerance`` is the fraction of pixels that may differ (default 2%).
    ``intensity_threshold`` is the per-channel max-diff under which pixels
    are considered equal (default 16/255 — absorbs sub-pixel font noise).
    """
    fraction, overlay = png_diff_fraction(actual_path, expected_path, intensity_threshold)
    if fraction <= tolerance:
        return
    overlay.save(out_dir / f"{name}.diff.png")
    shutil.copy(actual_path, out_dir / f"{name}.actual.png")
    shutil.copy(expected_path, out_dir / f"{name}.expected.png")
    raise AssertionError(
        f"{name} render diverged: {fraction:.2%} of pixels differ "
        f"(tolerance {tolerance:.0%}). "
        f"Inspect {name}.{{actual,expected,diff}}.png in {out_dir}."
    )
