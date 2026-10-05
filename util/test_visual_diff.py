from pathlib import Path

import pytest_bazel
from PIL import Image

from util.visual_diff import compare_pngs


def _png(path: Path, color: tuple[int, int, int, int] = (10, 20, 30, 255), size: tuple[int, int] = (8, 8)) -> Path:
    Image.new("RGBA", size, color).save(path)
    return path


def test_compare_pngs_classifies_exact_diffs(tmp_path: Path) -> None:
    identical = _png(tmp_path / "identical.png")
    assert compare_pngs(_png(tmp_path / "a.png"), identical).classification == "unchanged"

    modified = compare_pngs(_png(tmp_path / "a.png"), _png(tmp_path / "b.png", (10, 20, 31, 255)))
    assert modified.classification == "modified"
    assert modified.changed_pixels == 64
    assert modified.dimension_changed is False
    assert modified.diff_overlay is not None

    resized = compare_pngs(_png(tmp_path / "a.png"), _png(tmp_path / "c.png", size=(8, 10)))
    assert resized.classification == "modified"
    assert resized.dimension_changed is True


if __name__ == "__main__":
    pytest_bazel.main()
