"""Compare decoded RGB pixels and paint changes red for visual reviews."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops


def _rgb_difference_mask(actual: Image.Image, baseline: Image.Image) -> Image.Image:
    # Use the maximum channel difference so a change in any RGB channel is counted.
    diff = ImageChops.difference(actual.convert("RGB"), baseline.convert("RGB"))
    red, green, blue = diff.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue).point(lambda value: 255 if value else 0)


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
    """Compare the RGB channels exactly and mark a size change as modified."""
    with Image.open(actual) as candidate, Image.open(baseline) as before:
        actual_img = candidate.convert("RGBA")
        baseline_img = before.convert("RGBA")
    actual_size, baseline_size = actual_img.size, baseline_img.size
    dimension_changed = actual_size != baseline_size

    if dimension_changed:
        canvas_size = (max(actual_size[0], baseline_size[0]), max(actual_size[1], baseline_size[1]))
        actual_canvas = Image.new("RGBA", canvas_size)
        actual_canvas.paste(actual_img, (0, 0))
        baseline_canvas = Image.new("RGBA", canvas_size)
        baseline_canvas.paste(baseline_img, (0, 0))
        actual_img, baseline_img = actual_canvas, baseline_canvas

    mask = _rgb_difference_mask(actual_img, baseline_img)
    differing = mask.histogram()[255]
    if differing == 0 and not dimension_changed:
        return PngComparison("unchanged", 0.0, 0, actual_size, baseline_size, False)

    width, height = actual_img.size
    actual_img.paste((255, 0, 0, 255), (0, 0, width, height), mask)
    return PngComparison(
        classification="modified",
        changed_fraction=differing / (width * height),
        changed_pixels=differing,
        actual_size=actual_size,
        baseline_size=baseline_size,
        dimension_changed=dimension_changed,
        diff_overlay=actual_img,
    )
