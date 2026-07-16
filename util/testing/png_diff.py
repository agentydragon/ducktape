"""Pixel-tolerant PNG golden comparison with debug-artifact dumping.

Designed for visual regression tests that screenshot real GUIs (gnome-shell,
FreeCAD, etc.) where sub-pixel font rasterization noise can drift across runs
even when nothing meaningful changed.

`assert_png_matches_golden` writes `<name>.{actual,expected,diff}.png` to a
caller-supplied output dir on failure (typically the bazel undeclared outputs
dir, so the artifacts ride out to BuildBuddy). The pixel-diff math itself lives
in :mod:`util.visual_diff`, shared with the trusted visual-review publisher.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from util.visual_diff import png_diff_fraction


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
