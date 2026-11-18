from pathlib import Path

import pytest_bazel
from PIL import Image

from devinfra.pr_visuals.visual_diff import compare_pngs


def _png(path: Path, color: tuple[int, int, int, int] = (10, 20, 30, 255), size: tuple[int, int] = (8, 8)) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


def test_compare_pngs_classifies_exact_diffs(tmp_path: Path) -> None:
    baseline = _png(tmp_path / "baseline.png")
    candidate = _png(tmp_path / "candidate.png", (10, 20, 30, 128))
    assert compare_pngs(candidate, baseline).classification == "unchanged"

    # A one-unit change in a single channel counts as one pixel.
    changed = Image.new("RGBA", (8, 8), (10, 20, 30, 255))
    changed.putpixel((0, 0), (10, 20, 31, 255))
    changed.save(candidate)
    modified = compare_pngs(candidate, baseline)
    assert modified.classification == "modified"
    assert modified.changed_pixels == 1
    assert modified.changed_fraction == 1 / 64
    assert modified.dimension_changed is False
    assert modified.diff_overlay is not None
    assert modified.diff_overlay.getpixel((0, 0)) == (255, 0, 0, 255)
    assert modified.diff_overlay.getpixel((1, 0)) == (10, 20, 30, 255)

    resized = compare_pngs(_png(tmp_path / "resized.png", size=(8, 10)), baseline)
    assert resized.classification == "modified"
    assert resized.dimension_changed is True
    assert resized.changed_pixels == 16
    assert resized.changed_fraction == 16 / 80


if __name__ == "__main__":
    pytest_bazel.main()
