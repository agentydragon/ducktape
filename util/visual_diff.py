"""Exact-pixel PNG comparison for visual-review publishing.

The pixel math is shared by the trusted visual-review publisher (production) and
the golden-comparison test helper in ``util.testing.png_diff``. Decoded pixels
are compared via a per-channel L-infinity norm: collapsing to luminance would
apply ITU-R weights and under-count pure red/blue diffs (a 50-unit red change
drops to ~15 luminance, slipping under typical thresholds), so the channel-wise
max is taken instead.

Comparison is on decoded RGB channels. Every producer in this pipeline emits
fully opaque screenshots (constant alpha), so alpha carries no information and is
ignored; two images with identical RGB but differing alpha classify as unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops

THRESHOLD_ANY_DIFF = 1
"""Per-channel diff at which a pixel counts as changed for exact comparison.

``1`` flags every nonzero channel difference, matching the exact classification
(no tolerance). Lower would flag nothing; higher would silently ignore diffs.
"""


def _per_pixel_max_channel_diff(actual: Image.Image, expected: Image.Image) -> Image.Image:
    """Return an L-mode image of the per-pixel max RGB channel difference.

    ``ImageChops.difference`` returns the absolute per-channel diff; collapsing
    via ``convert("L")`` would apply ITU-R luminance weights and under-count
    pure-red or pure-blue diffs (a 50-unit red change drops to 50*0.299=15
    luminance, slipping under typical thresholds). Take channel-wise max
    instead, which is the per-pixel L-infinity norm.
    """
    diff = ImageChops.difference(actual.convert("RGB"), expected.convert("RGB"))
    r, g, b = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _red_overlay(base: Image.Image, max_channel_diff: Image.Image, intensity_threshold: int) -> tuple[Image.Image, int]:
    """Paint differing pixels of ``base`` bright red; return (overlay, differing count).

    ``max_channel_diff`` is an L-mode per-pixel diff; pixels at or above
    ``intensity_threshold`` are painted red on a copy of ``base``. The count
    comes from the threshold mask histogram (bucket 255), avoiding a full buffer.
    """
    mask = max_channel_diff.point(lambda value: 255 if value >= intensity_threshold else 0)
    overlay = base.copy()
    red = Image.new("RGBA", base.size, (255, 0, 0, 255))
    overlay.paste(red, mask=mask)
    return overlay, mask.histogram()[255]


def png_diff_fraction(actual_path: Path, expected_path: Path, intensity_threshold: int) -> tuple[float, Image.Image]:
    """Diff two same-sized PNGs; return ``(fraction_differing, red_overlay)``.

    The overlay is the actual image with pixels whose per-channel max diff is at
    least ``intensity_threshold`` painted bright red — useful for eyeball
    debugging. Requires equal dimensions; raises on mismatch (use
    :func:`compare_pngs` for dimension-tolerant comparison).
    """
    actual = Image.open(actual_path).convert("RGBA")
    expected = Image.open(expected_path).convert("RGBA")
    if actual.size != expected.size:
        raise AssertionError(f"PNG size mismatch: actual={actual.size} expected={expected.size}")

    overlay, differing = _red_overlay(actual, _per_pixel_max_channel_diff(actual, expected), intensity_threshold)
    fraction = differing / (actual.size[0] * actual.size[1])
    return fraction, overlay


@dataclass(frozen=True)
class PngComparison:
    classification: Literal["unchanged", "modified"]
    changed_fraction: float
    changed_pixels: int
    actual_size: tuple[int, int]
    baseline_size: tuple[int, int]
    dimension_changed: bool
    diff_overlay: Image.Image | None = None


def compare_pngs(actual: Path, baseline: Path) -> PngComparison:
    """Exact-pixel comparison of candidate ``actual`` against ``baseline``.

    Same dimensions and byte-identical decoded RGB → ``unchanged``. Any differing
    pixel, or a dimension change, → ``modified``. For modified results the
    ``diff_overlay`` is a red-where-changed visualization; for unchanged it is
    ``None`` (no overlay is needed or written). Dimension changes paste both
    images onto a shared transparent canvas before diffing, so the non-overlap
    region surfaces as changed.
    """
    actual_img = Image.open(actual).convert("RGBA")
    baseline_img = Image.open(baseline).convert("RGBA")
    actual_size, baseline_size = actual_img.size, baseline_img.size
    dimension_changed = actual_size != baseline_size

    if not dimension_changed and actual_img.convert("RGB").tobytes() == baseline_img.convert("RGB").tobytes():
        return PngComparison("unchanged", 0.0, 0, actual_size, baseline_size, False)

    if dimension_changed:
        canvas_size = (max(actual_size[0], baseline_size[0]), max(actual_size[1], baseline_size[1]))
        actual_canvas = Image.new("RGBA", canvas_size)
        actual_canvas.paste(actual_img, (0, 0))
        baseline_canvas = Image.new("RGBA", canvas_size)
        baseline_canvas.paste(baseline_img, (0, 0))
        overlay, differing = _red_overlay(
            actual_canvas, _per_pixel_max_channel_diff(actual_canvas, baseline_canvas), THRESHOLD_ANY_DIFF
        )
        total = canvas_size[0] * canvas_size[1]
    else:
        fraction, overlay = png_diff_fraction(actual, baseline, THRESHOLD_ANY_DIFF)
        total = actual_size[0] * actual_size[1]
        differing = round(fraction * total)

    return PngComparison(
        classification="modified",
        changed_fraction=differing / total,
        changed_pixels=differing,
        actual_size=actual_size,
        baseline_size=baseline_size,
        dimension_changed=dimension_changed,
        diff_overlay=overlay,
    )
